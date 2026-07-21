#!/usr/bin/env python3
"""Cumulative Stage-1 restart/replay gate.

Drives the real ``Stage1RunService`` composition the ``enginery`` CLI uses
(``enginery.cli.stage1._advancing_service``) through two independent local
work items on one durable SQLite ledger, injecting a coordinator restart --
closing the ledger handle and every in-memory ``CoordinatorRuntime`` /
``Stage1RunService`` object, then reopening a brand-new set against the same
on-disk file -- between every externally observable step. This is the same
restart-proof convention already used throughout this repository's merged
recovery tests and stress scripts (for example
``tests/engine/test_plan_execution.py``,
``tests/stacks/test_stack_coordinator.py``,
``scripts/stress_plan_scheduler.py``): a fresh runtime object reconstructed
from durable ledger state stands in for a real process replacement, per the
recovery topology in the system design's coordinator-epoch/fencing model.
It is *not* a literal new operating-system process boundary; the
release-readiness report must describe it precisely as such.

Qualification, implementation dispatch, and validation are completed
through direct durable node-state transitions rather than the real
``Stage1QualificationExecutor``/``Stage1ImplementationExecutor``/
``Stage1ValidationExecutor`` -- exactly the pattern
``tests/outcomes/test_dogfooding.py`` already uses for a fully local,
no-live-provider proof. Those three nodes' own crash/fault-injection
coverage is already established by the merged M8 corrective stack; this
gate's job is the review -> pull-request -> CI-evidence ->
merge-ready-verification -> outcome-registration chain, which is where a
real Stage 1 run's durable, externally observable claims live. Every step
in that chain runs through the same public ``Stage1RunService`` methods
the CLI calls (``review_implementation``, ``open_pull_request``,
``wait_for_ci``, ``verify_merge_ready``, ``advance``), with the pull-request
port implemented by a deterministic in-script fixture (no live GitHub
credentials; live-provider Stage 1 recovery was already independently
proven and retained at issue #84 -> PR #90 in this repository).

Only Stage 1 is supported. ``--stages`` must be exactly ``1``; Stage 2-4
cumulative gates belong to their own release trains (``v0.2.0``,
``v0.3.0``, and the gate-deferred Stage 4 train), not to this milestone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from enginery.application.work_ports import (
    LifecycleProjection,
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestPort,
    PullRequestRequest,
    PullRequestSnapshot,
    WorkLedgerPort,
    WorkLedgerSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import EngineryError
from enginery.domain.ids import NodeId, OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.review import ReviewFinding, ReviewReport
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
)

_HEARTBEAT = timedelta(seconds=60)
_LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class _TerminalPullRequests:
    """A deterministic local pull-request/CI fixture bound to one work item."""

    def __init__(self, *, pull_request_number: int) -> None:
        self.pull_request_number = pull_request_number
        self.requests: list[PullRequestRequest] = []

    def probe(self) -> object:
        raise AssertionError("not used by this gate")

    def create_or_update(self, request: PullRequestRequest) -> PullRequestSnapshot:
        self.requests.append(request)
        return self._snapshot(request.head_branch, request.base_branch)

    def get(self, number: int) -> PullRequestSnapshot:
        if number != self.pull_request_number or not self.requests:
            raise AssertionError("unexpected pull request lookup")
        last = self.requests[-1]
        return self._snapshot(last.head_branch, last.base_branch)

    def evidence(self, number: int) -> PullRequestEvidence:
        snapshot = self.get(number)
        return PullRequestEvidence(
            pull_request=snapshot,
            reviews=(),
            checks=(
                PullRequestCheck(
                    name="CI",
                    status="completed",
                    conclusion="success",
                    head_revision=snapshot.head_revision,
                ),
            ),
            mergeable=True,
        )

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return ReconciliationResult.FOUND_MATCHING

    def _snapshot(self, head_branch: str, base_branch: str) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            number=self.pull_request_number,
            url=f"https://example.test/pull/{self.pull_request_number}",
            state="open",
            head_branch=head_branch,
            head_revision="head-revision",
            base_branch=base_branch,
            base_revision="base-revision",
        )


class _TerminalWorkLedger:
    """A deterministic local work-ledger fixture that records every publish."""

    def __init__(self, snapshot: WorkLedgerSnapshot) -> None:
        self.snapshot = snapshot
        self.published: list[LifecycleProjection] = []

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        if external_reference != self.snapshot.work_item.external_reference:
            raise AssertionError("unexpected external reference")
        return self.snapshot

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        self.published.append(projection)
        return ReconciliationResult.FOUND_MATCHING


@dataclass(frozen=True, slots=True)
class _RunFixture:
    """Everything one cumulative Stage-1 gate run needs, held outside the ledger."""

    request: Stage1RunRequest
    work_ledger: WorkLedgerPort
    pull_requests: PullRequestPort


def _snapshot(run_index: int) -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId(f"work-{run_index}"),
            work_kind=WorkKind.ISSUE,
            source_provider="local",
            external_reference=f"local-issue:{run_index}",
            source_snapshot_reference=f"local-issue:{run_index}@1",
            title="Bounded gate change",
            objective="Change one bounded behavior for the cumulative Stage-1 gate.",
            acceptance_criteria=("observable result",),
            constraints=("retain evidence",),
            risk_class=RiskClass.LOW,
            repository_targets=(f"repository-{run_index}",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="1",
    )


def _build_fixture(run_index: int, artifact_root: Path) -> _RunFixture:
    run_id = f"run-gate-{run_index}"
    manifest = issue_to_pr_manifest()
    manifest = replace(
        manifest,
        nodes={
            **manifest.nodes,
            NodeId("implement"): replace(
                manifest.nodes[NodeId("implement")], actor_type=ActorType.DETERMINISTIC
            ),
        },
    )
    snapshot = _snapshot(run_index)
    request = Stage1RunRequest(
        run=Run(
            id=RunId(run_id),
            work_item_id=snapshot.work_item.id,
            work_item_snapshot_digest=snapshot.work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository=f"repository-{run_index}",
            base_revision="base-revision",
            policy_set_version="policy-v1",
            adapter_versions={},
            adapter_fingerprints={},
            capability_lock_digest=Digest.of_bytes(b"capability-lock"),
            environment_manifest_digest=Digest.of_bytes(b"environment"),
            configuration_snapshot_digest=Digest.of_bytes(b"configuration"),
            state=RunState.CREATED,
        ),
        work_snapshot=snapshot,
        manifest=manifest,
        repository_id=f"repository-{run_index}",
        repository_path=(artifact_root / f"repository-{run_index}").resolve(),
        workspace_path=(artifact_root / f"workspace-{run_index}").resolve(),
        base_branch="main",
        head_branch=f"enginery/{run_id}",
        validation_commands=(("true",),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId(f"implement:{run_id}"),
            time_budget_seconds=60,
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="local/full-system-gate-fixture",
            github_credential_reference="unused-local-fixture",
            github_executable="true",
            harness_provider="omp",
            harness_credential_reference="unused-local-fixture",
            harness_executable="true",
            artifact_root=(artifact_root / f"artifacts-{run_index}").resolve(),
        ),
    )
    return _RunFixture(
        request=request,
        work_ledger=cast(WorkLedgerPort, _TerminalWorkLedger(snapshot)),
        pull_requests=cast(
            PullRequestPort, _TerminalPullRequests(pull_request_number=100 + run_index)
        ),
    )


def _service(
    database: Path, *, work_ledger: WorkLedgerPort, pull_requests: PullRequestPort
) -> tuple[LedgerService, Stage1RunService]:
    """Open a fresh ledger handle and coordinator/service pair.

    Called once per gate phase when ``restart_between_stages`` is set: the
    caller closes the previous ledger handle first, so this reconstructs
    every in-memory object from durable state alone, exactly like a real
    coordinator process replacement.
    """
    ledger = LedgerService.open(database)
    runtime = CoordinatorRuntime(ledger, owner="full-system-gate")
    service = Stage1RunService(
        runtime=runtime,
        ledger=ledger,
        work_ledger=work_ledger,
        pull_requests=pull_requests,
        outcomes=OutcomeCaptureService(ledger=ledger, pull_requests=pull_requests),
    )
    return ledger, service


def _complete_bypassed_nodes(service: Stage1RunService, fixture: _RunFixture) -> None:
    """Durably complete qualify/implement/validate outside the real executors.

    Matches ``tests/outcomes/test_dogfooding.py``: those three nodes' own
    crash/fault-injection coverage already lives in the merged M8
    corrective stack. This gate exercises the review-through-outcome chain,
    which is where a real Stage 1 run's externally observable claims live.
    """
    request = fixture.request
    service.start(request, now=_NOW, heartbeat_window=_HEARTBEAT)
    dependency_by_node = {
        "qualify": (),
        "implement": ((str(request.run.id), "qualify"),),
        "validate": ((str(request.run.id), "implement"),),
    }
    for node_id, dependencies in dependency_by_node.items():
        attempt_id = f"{request.run.id}-{node_id}-0"
        dispatch = WorkflowNodeDispatch(
            FixtureDispatch(
                run_id=str(request.run.id),
                node_id=node_id,
                attempt_id=attempt_id,
                repository_id=request.repository_id,
                repository_path=request.repository_path,
                workspace_path=request.workspace_path,
                base_revision="base-revision",
                command=(node_id,),
                expected_attempt_version=0,
                operation_id=f"{node_id}:{request.run.id}",
                dependencies=dependencies,
                workflow_definition_id=request.manifest.id.value,
            ),
            request.manifest,
        )
        epoch = service.runtime.register_node(
            dispatch=dispatch, now=_NOW, heartbeat_window=_HEARTBEAT
        )
        service.runtime.complete_node(
            run_id=str(request.run.id),
            node_id=node_id,
            epoch=epoch.epoch,
            now=_NOW,
            extra=(
                {"validation_artifact_digest": str(Digest.of_bytes(b"validation"))}
                if node_id == "validate"
                else None
            ),
        )
    service.ledger.append(
        AppendCommand(
            correlation_id=f"implementation-artifacts-{request.run.id}",
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=f"{request.run.id}-implement-0",
                    expected_version=0,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={"artifact_references": [str(Digest.of_bytes(b"implementation"))]},
                ),
            ),
        )
    )


def _drive_to_terminal(
    database: Path, fixture: _RunFixture, *, restart_between_stages: bool
) -> dict[str, object]:
    """Drive one work item from durable-node bypass through outcome registration.

    When ``restart_between_stages`` is set, the coordinator/ledger handle is
    closed and reopened from durable state before every externally
    observable step -- a real process replacement. Otherwise one continuous
    ledger session drives the whole sequence, which still proves cumulative
    multi-run correctness but not restart recovery.
    """
    run_id = fixture.request.run.id

    def reopen() -> tuple[LedgerService, Stage1RunService]:
        return _service(
            database, work_ledger=fixture.work_ledger, pull_requests=fixture.pull_requests
        )

    def advance_and_expect(
        service: Stage1RunService, expected_action: str, *, description: str
    ) -> None:
        progression = service.advance(
            run_id, now=_NOW, heartbeat_window=_HEARTBEAT, lease_window=_HEARTBEAT, limits=_LIMITS
        )
        if progression.action.value != expected_action:
            raise AssertionError(f"expected {description}, got {progression.action.value}")

    ledger, service = reopen()
    try:
        _complete_bypassed_nodes(service, fixture)
    finally:
        ledger.close()

    ledger, service = reopen()
    try:
        review_result = service.review_implementation(
            fixture.request,
            ReviewReport(
                producer="local-fixture-agent",
                reviewer="operator",
                findings=(ReviewFinding("format", actionable=False, blocking=False),),
            ),
            repair_attempt=0,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        if review_result.outcome.value != "approved":
            raise AssertionError(f"expected approved review, got {review_result.outcome.value}")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen()
        advance_and_expect(service, "wait_for_ci", description="open_pr then wait_for_ci")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen()
        advance_and_expect(service, "verify", description="wait_for_ci then verify")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen()
        advance_and_expect(
            service, "register_outcome_observation", description="verify then register"
        )

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen()
        advance_and_expect(service, "wait", description="terminal wait")

        # Idempotency: re-deriving the next action after the run is terminal
        # must never re-dispatch a completed side effect.
        repeated = service.next_action(run_id)
        if repeated.action.value != "wait":
            raise AssertionError("terminal run must remain idempotently at wait")

        run = service.read(run_id)
        pull_requests = cast(_TerminalPullRequests, fixture.pull_requests)
        evidence: dict[str, object] = {
            "run_id": str(run_id),
            "status": run.status.value,
            "aggregate_version": run.aggregate_version,
            "request_digest": str(run.request.digest),
            "pull_request_requests": len(pull_requests.requests),
        }
    finally:
        ledger.close()

    pull_requests = cast(_TerminalPullRequests, fixture.pull_requests)
    if len(pull_requests.requests) != 1:
        raise AssertionError(
            "restart/replay must not create a duplicate pull request: "
            f"observed {len(pull_requests.requests)} create_or_update calls"
        )
    return evidence


@dataclass(slots=True)
class GateReport:
    stages: str
    restart_between_stages: bool
    run_evidence: list[dict[str, object]] = field(default_factory=list)
    evidence_digest: str = ""


def run_gate(
    *, stages: str = "1", restart_between_stages: bool = True, run_count: int = 2
) -> GateReport:
    if stages != "1":
        raise EngineryError(
            "full_system_gate.py only supports --stages 1 in this milestone; "
            "Stage 2-4 cumulative gates belong to their own release trains",
            details={"requested_stages": stages},
        )
    if run_count < 2:
        raise EngineryError(
            "the cumulative gate requires at least two runs on one durable ledger",
            details={"run_count": run_count},
        )
    report = GateReport(stages=stages, restart_between_stages=restart_between_stages)
    with TemporaryDirectory(prefix="enginery-full-system-gate-") as tmp:
        artifact_root = Path(tmp)
        database = artifact_root / "ledger.db"
        for run_index in range(1, run_count + 1):
            fixture = _build_fixture(run_index, artifact_root)
            evidence = _drive_to_terminal(
                database, fixture, restart_between_stages=restart_between_stages
            )
            report.run_evidence.append(evidence)
    report.evidence_digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(report.run_evidence, sort_keys=True).encode()).hexdigest()
    )
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cumulative Stage-1 restart/replay gate (local fixtures only)."
    )
    parser.add_argument(
        "--stages",
        default="1",
        help="comma-separated stages to gate; only '1' is supported in this milestone",
    )
    parser.add_argument(
        "--restart-between-stages",
        action="store_true",
        help="reconstruct the coordinator/ledger handle between every step",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_gate(stages=args.stages, restart_between_stages=args.restart_between_stages)
    except EngineryError as error:
        print(f"FAIL full-system-gate: {error}", file=sys.stderr)
        return 1
    except AssertionError as error:
        print(f"FAIL full-system-gate: {error}", file=sys.stderr)
        return 1
    print(
        "PASS full-system-gate "
        f"stages={report.stages} restart_between_stages={report.restart_between_stages} "
        f"runs={len(report.run_evidence)} evidence_digest={report.evidence_digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
