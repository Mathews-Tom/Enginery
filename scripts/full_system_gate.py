#!/usr/bin/env python3
"""Cumulative Stage-1/Stage-2 restart/replay gate.

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

``--stages`` accepts any combination of ``1``, ``2``, and ``3`` (for
example ``1``, ``1,2``, or ``1,2,3``). The Stage 2 leg drives the
real ``Stage2ReleaseWorkflow`` (``enginery.workflows.plan_to_release``)
through its full merge -> prepare -> build -> publish -> verify sequence
against a disposable local fixture package: the merge phase uses a real,
ledger-backed ``StackCoordinator``/``MergePolicyService`` pair (restart is
proven the same way as the Stage 1 leg -- close and reopen the ledger
handle, then re-read durable stack state); the build phase runs a real
``uv build`` and a real isolated-venv clean install, exactly like a live
release, with no network access at all; the publish phase runs the real
``GitHubReleaseAdapter``/``PyPiAdapter`` code paths against injected fake
command runners standing in for ``gh``/``uv publish`` and the PyPI JSON
API, so no live GitHub or PyPI credential or network call is ever made.
``restart_between_stages`` additionally proves publish-side idempotent
replay: after the first publish, the two adapter objects are rebuilt from
scratch (discarding their in-memory state, matching a real process
restart) and ``publish()`` is called again against the *same* fake
destination-server state (which, like a real external service, persists
across the simulated restart); the gate fails if that replay creates a
second GitHub release or reports a different artifact digest. Policy
approval for ``release.publish`` is recorded once against one
``ApprovalRegistry``/``PolicyEvaluator`` pair that is *not* itself
reconstructed between stages -- durable policy-decision persistence
across a real restart is Stage 2's own, separately covered surface
(``tests/policy``, ``tests/workflows/test_plan_to_release.py``), not
re-proven here.

The Stage 3 leg drives the real, ledger-backed ``IncidentService``
(``enginery.incidents.service``) through ingest -> classify -> bind
release lineage -> reproduce -> hotfix -> deploy -> observe -> roll
back -> restore -> follow-up, reusing the exact same real
``LocalServiceDeploymentAdapter`` (a real subprocess-managed local HTTP
service, not a fake) and ``enginery.incidents.hotfix`` git-worktree
code paths ``scripts/run_stage3_gate.py`` already exercises. Unlike
the Stage 2 leg, no external credential, fake command runner, or
network call is involved at all -- the controlled target is a real
process on ``127.0.0.1``. ``restart_between_stages`` closes and
reopens the ``IncidentService``'s ledger handle before every
externally observable step, proving the incident's durable state
(including its terminal ``rolled_back`` outcome and authority-record
count) survives a coordinator restart; the deployment broker, policy
evaluator, and approval registry are held constant across the
restart, matching the Stage 2 leg's own documented policy-persistence
caveat above.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubReleaseAdapter,
    GitHubReleaseRequest,
)
from enginery.adapters.local import LocalValidation
from enginery.adapters.local_service import (
    LocalServiceBuild,
    LocalServiceDeploymentAdapter,
    build_local_service_artifact,
)
from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.delivery_ports import DeploymentRequest
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
from enginery.domain.ids import (
    NodeId,
    OperationId,
    PlanId,
    PlanMilestoneId,
    RunId,
    StackId,
    WorkflowDefinitionId,
    WorkItemId,
)
from enginery.domain.incident import (
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    ReproductionOutcome,
    ReproductionRecord,
)
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.node import ActorType
from enginery.engine.fixture_build import FixtureBuilder
from enginery.engine.release_manifest import ReleaseManifest, ReleaseTarget, VersionChangelogBroker
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.incidents.hotfix import (
    HotfixRepair,
    apply_repair,
    create_hotfix_worktree,
    prove_non_vacuous_regression,
    remove_hotfix_worktree,
)
from enginery.incidents.service import IncidentService
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator, PolicyRule
from enginery.policy.schemas import ApprovalSchema
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.merge_policy import MergePolicyService
from enginery.workflows.plan_to_release import Stage2ReleaseWorkflow
from enginery.workflows.review import ReviewFinding, ReviewOutcome, ReviewReport, route_review
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
)

_HEARTBEAT = timedelta(seconds=60)
_LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_SUPPORTED_STAGES = frozenset({"1", "2", "3"})
_STAGE2_MILESTONES = (
    (PlanMilestoneId("gate-m1"), "fixture/gate-m1"),
    (PlanMilestoneId("gate-m2"), "fixture/gate-m2"),
)
_STAGE2_DISTRIBUTION_NAME = "enginery-full-system-gate-fixture"
_STAGE2_IMPORT_MODULE = "enginery_full_system_gate_fixture"
_STAGE2_VERSION = "0.0.1"
_STAGE3_APP_SCRIPT = (
    Path(__file__).resolve().parent.parent / "fixtures" / "enginery-stage3-local-service" / "app.py"
)
_STAGE3_BUGGY_APP = "def add(a, b):\n    return a + b + 1\n"
_STAGE3_FIXED_APP = "def add(a, b):\n    return a + b\n"
_STAGE3_CHECK_COMMAND = ("python3", "-c", "exec(open('app.py').read()); assert add(2, 3) == 5")
_STAGE3_HUMAN = AuthorityPrincipal(
    id="full-system-gate-operator",
    principal_type=PrincipalType.HUMAN,
    role="operator",
    authorization_source="full-system-gate",
)
_STAGE3_REQUESTING_PRINCIPAL_ID = "full-system-gate-incident-workflow"


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


def _build_stage2_fixture_package(root: Path) -> None:
    """Write a disposable, buildable local package -- never published anywhere.

    Mirrors ``fixtures/enginery-stage2-fixture``'s own shape (hatchling
    backend, ``importlib.metadata``-sourced ``__version__``) under a
    distinctly named distribution so it can never be confused with that
    real, already-published fixture.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{_STAGE2_DISTRIBUTION_NAME}"\n'
        'version = "0.0.0"\n'
        'description = "Disposable local full-system-gate Stage 2 fixture. Never published."\n'
        'requires-python = ">=3.12"\n'
        "\n"
        "[build-system]\n"
        'requires = ["hatchling>=1.25"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{_STAGE2_IMPORT_MODULE}"]\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    package_dir = root / "src" / _STAGE2_IMPORT_MODULE
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text(
        "from __future__ import annotations\n\n"
        "from importlib.metadata import PackageNotFoundError, version\n\n"
        "try:\n"
        f'    __version__ = version("{_STAGE2_DISTRIBUTION_NAME}")\n'
        "except PackageNotFoundError:\n"
        '    __version__ = "0.0.0.dev0"\n',
        encoding="utf-8",
    )


class _Stage2PullRequests:
    """A deterministic local pull-request fixture bound to the Stage 2 merge phase."""

    def __init__(self) -> None:
        self._evidence_by_number: dict[int, PullRequestEvidence] = {}
        self._merged: dict[int, PullRequestSnapshot] = {}
        self.merge_calls: list[int] = []

    def register(
        self, number: int, evidence: PullRequestEvidence, merged: PullRequestSnapshot
    ) -> None:
        self._evidence_by_number[number] = evidence
        self._merged[number] = merged

    def probe(self) -> object:
        raise AssertionError("not used by this gate")

    def create_or_update(self, request: PullRequestRequest) -> PullRequestSnapshot:
        raise AssertionError("not used by this gate")

    def get(self, number: int) -> PullRequestSnapshot:
        raise AssertionError("not used by this gate")

    def evidence(self, number: int) -> PullRequestEvidence:
        return self._evidence_by_number[number]

    def merge(
        self,
        number: int,
        *,
        expected_head_revision: str,
        operation_id: OperationId,
        merge_method: str = "merge",
    ) -> PullRequestSnapshot:
        self.merge_calls.append(number)
        return self._merged[number]

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return ReconciliationResult.NOT_FOUND


def _stage2_pr_evidence(*, number: int, head: str, base: str) -> PullRequestEvidence:
    snapshot = PullRequestSnapshot(
        number=number,
        url=f"https://example.test/pull/{number}",
        state="open",
        head_branch=f"fixture/gate-m{number}",
        head_revision=head,
        base_branch=base,
        base_revision="b" * 40,
    )
    return PullRequestEvidence(
        pull_request=snapshot,
        reviews=(),
        checks=(
            PullRequestCheck(
                name="CI", status="completed", conclusion="success", head_revision=head
            ),
        ),
        mergeable=True,
    )


def _stage2_pr_merged(*, number: int, head: str, base: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=number,
        url=f"https://example.test/pull/{number}",
        state="closed",
        head_branch=f"fixture/gate-m{number}",
        head_revision=head,
        base_branch=base,
        base_revision="b" * 40,
        merged=True,
    )


class _FakeGitHubReleaseServer:
    """Stands in for the real GitHub Release API across a simulated restart.

    Deliberately persists across adapter reconstruction: a real GitHub
    repository's release state outlives any one coordinator process, so
    proving idempotent replay requires the *server*, not the adapter, to
    remember what already exists.
    """

    def __init__(self) -> None:
        self._releases: dict[str, dict[str, object]] = {}

    @property
    def release_count(self) -> int:
        return len(self._releases)

    def command_runner(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if len(arguments) == 2 and arguments[1] == "--version":
            return subprocess.CompletedProcess(
                arguments, 0, stdout="gh version 0.0.0 (local fixture)", stderr=""
            )
        if len(arguments) < 9 or arguments[1] != "api":
            raise AssertionError(
                f"unexpected gh invocation for the local Stage 2 gate: {arguments}"
            )
        method = arguments[3]
        endpoint = arguments[8]
        if method == "GET":
            tag_name = urllib.parse.unquote(endpoint.rsplit("/", 1)[-1])
            release = self._releases.get(tag_name)
            if release is None:
                return subprocess.CompletedProcess(
                    arguments, 1, stdout="", stderr="HTTP 404: Not Found"
                )
            return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(release), stderr="")
        if method == "POST":
            fields: dict[str, str] = {}
            for index in range(9, len(arguments), 2):
                key, _, value = arguments[index + 1].partition("=")
                fields[key] = value
            tag_name = fields["tag_name"]
            release = {
                "tag_name": tag_name,
                "target_commitish": fields["target_commitish"],
                "name": fields.get("name", ""),
                "body": fields.get("body", ""),
                "draft": False,
                "prerelease": fields.get("prerelease") == "true",
            }
            self._releases[tag_name] = release
            return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(release), stderr="")
        raise AssertionError(f"unexpected GitHub API method for the local Stage 2 gate: {method}")


class _FakePyPiServer:
    """Stands in for a real PyPI-compatible index across a simulated restart."""

    def __init__(self) -> None:
        self._digest_by_version: dict[str, str] = {}

    def published_versions(self) -> list[str]:
        return sorted(self._digest_by_version)

    def command_runner(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if tuple(command[:2]) != ("uv", "publish"):
            raise AssertionError(f"unexpected uv invocation for the local Stage 2 gate: {command}")
        path = Path(command[-1])
        version = path.name.removesuffix(".whl").split("-")[1]
        self._digest_by_version[version] = hashlib.sha256(path.read_bytes()).hexdigest()
        return subprocess.CompletedProcess(tuple(command), 0, stdout="", stderr="")

    def url_opener(self, url: str) -> bytes:
        version = url.rsplit("/", 2)[1]
        digest = self._digest_by_version.get(version)
        if digest is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        payload = {"urls": [{"digests": {"sha256": digest}}]}
        return json.dumps(payload).encode("utf-8")


def _run_stage2_leg(
    database: Path, fixture_root: Path, *, restart_between_stages: bool
) -> dict[str, object]:
    """Drive the real ``Stage2ReleaseWorkflow`` through merge -> publish -> verify.

    See the module docstring for exactly what ``restart_between_stages``
    proves for this leg (ledger-backed merge persistence and publish-side
    idempotent replay) and what it does not (policy/approval persistence,
    already covered elsewhere).
    """
    stack_id = StackId("gate-stage2-stack")
    human = AuthorityPrincipal(
        id="gate-operator",
        principal_type=PrincipalType.HUMAN,
        role="operator",
        authorization_source="full-system-gate",
    )
    registry = ApprovalRegistry(registered_humans=(human,))
    policy = PolicyEvaluator(
        policy_version="full-system-gate-1.0.0",
        rules=(
            PolicyRule(
                id="allow_merge",
                action=PolicyAction.PULL_REQUEST_MERGE,
                result=PolicyResult.ALLOW,
                rationale="local Stage 2 gate",
                risk_classes=frozenset({RiskClass.LOW}),
            ),
            PolicyRule(
                id="allow_prepare",
                action=PolicyAction.RELEASE_PREPARE,
                result=PolicyResult.ALLOW,
                rationale="local Stage 2 gate",
                risk_classes=frozenset({RiskClass.LOW}),
            ),
        ),
        approval_registry=registry,
    )
    github_server = _FakeGitHubReleaseServer()
    pypi_server = _FakePyPiServer()
    github_config = GitHubAdapterConfig(
        repository="local/full-system-gate-stage2-fixture",
        credential_reference="unused-local-fixture",
    )
    pypi_config = PyPiAdapterConfig(
        project_name=_STAGE2_DISTRIBUTION_NAME,
        index_url="https://pypi.example.test/simple/",
        publish_url="https://pypi.example.test/legacy/",
        json_api_base="https://pypi.example.test/pypi",
    )
    pull_requests = _Stage2PullRequests()
    pull_requests.register(
        201,
        _stage2_pr_evidence(number=201, head="gate-m1-rev1", base="main"),
        _stage2_pr_merged(number=201, head="gate-m1-rev1", base="main"),
    )
    pull_requests.register(
        202,
        _stage2_pr_evidence(number=202, head="gate-m2-rev1", base="fixture/gate-m1"),
        _stage2_pr_merged(number=202, head="gate-m2-rev1", base="fixture/gate-m1"),
    )
    _build_stage2_fixture_package(fixture_root)

    def reopen(owner: str) -> tuple[LedgerService, Stage2ReleaseWorkflow]:
        ledger = LedgerService.open(database)
        coordinator = StackCoordinator(ledger, CoordinatorRuntime(ledger, owner=owner))
        workflow = Stage2ReleaseWorkflow(
            merge_policy=MergePolicyService(
                stacks=coordinator,
                pull_requests=cast(PullRequestPort, pull_requests),
                policy=policy,
            ),
            release_manifest=VersionChangelogBroker(fixture_root=fixture_root),
            fixture_builder=FixtureBuilder(),
            github_release=GitHubReleaseAdapter(
                github_config, command_runner=github_server.command_runner
            ),
            pypi=PyPiAdapter(
                pypi_config,
                command_runner=pypi_server.command_runner,
                url_opener=pypi_server.url_opener,
            ),
            release_policy=policy,
        )
        return ledger, workflow

    ledger, workflow = reopen("full-system-gate-stage2")
    try:
        workflow.merge_policy.stacks.start(
            stack_id=stack_id,
            plan_id=PlanId("gate-plan-1"),
            base_ref="main",
            ordered_milestones=_STAGE2_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        workflow.merge_policy.stacks.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("gate-m1"),
            head_revision="gate-m1-rev1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        workflow.merge_policy.stacks.mark_merge_ready(
            stack_id,
            PlanMilestoneId("gate-m1"),
            head_revision="gate-m1-rev1",
            ci_evidence_digest=Digest.of_bytes(b"ci-gate-m1"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        workflow.merge_policy.stacks.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("gate-m2"),
            head_revision="gate-m2-rev1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        workflow.merge_policy.stacks.mark_merge_ready(
            stack_id,
            PlanMilestoneId("gate-m2"),
            head_revision="gate-m2-rev1",
            ci_evidence_digest=Digest.of_bytes(b"ci-gate-m2"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        merged_stack = workflow.merge_all(
            stack_id,
            pull_request_numbers={PlanMilestoneId("gate-m1"): 201, PlanMilestoneId("gate-m2"): 202},
            required_checks=("CI",),
            require_approved_review=False,
            risk_class=RiskClass.LOW,
            requesting_principal_id="gate-agent",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
    finally:
        ledger.close()

    if merged_stack.next_mergeable() is not None:
        raise AssertionError("expected the Stage 2 gate stack to be fully merged")
    if pull_requests.merge_calls != [201, 202]:
        raise AssertionError("expected exactly one root-to-leaf merge call per slice")

    if restart_between_stages:
        ledger, workflow = reopen("full-system-gate-stage2-restart")
        try:
            reread_stack = workflow.merge_policy.stacks.read(stack_id)
            if reread_stack is None or reread_stack.next_mergeable() is not None:
                raise AssertionError("merged stack state did not survive a coordinator restart")
        finally:
            ledger.close()

    ledger, workflow = reopen("full-system-gate-stage2-prepare")
    try:
        manifest = ReleaseManifest(
            target=ReleaseTarget(
                distribution_name=_STAGE2_DISTRIBUTION_NAME, version=_STAGE2_VERSION
            ),
            changelog_entry="Local full-system-gate Stage 2 fixture release.",
        )
        workflow.prepare_release(
            manifest,
            stack=merged_stack,
            risk_class=RiskClass.LOW,
            requesting_principal_id="gate-agent",
        )
        artifacts = workflow.build_and_verify_fixture(
            fixture_root, expected_version=_STAGE2_VERSION, import_module=_STAGE2_IMPORT_MODULE
        )
    finally:
        ledger.close()

    github_request = GitHubReleaseRequest(
        tag_name=f"{_STAGE2_DISTRIBUTION_NAME}-v{_STAGE2_VERSION}",
        target_commitish="f" * 40,
        name=f"v{_STAGE2_VERSION}",
        body="Local full-system-gate Stage 2 fixture release.",
    )
    schema = ApprovalSchema(
        action=PolicyAction.RELEASE_PUBLISH,
        risk_class=RiskClass.LOW,
        target_resource=github_request.tag_name,
        diff_or_artifact_digest=str(artifacts.wheel.digest),
        requesting_principal_id="gate-agent",
    )
    registry.record_approval(schema, approvers=(human,))

    ledger, workflow = reopen("full-system-gate-stage2-publish")
    try:
        pypi_receipt, github_receipt = workflow.publish(
            artifacts,
            run_id=RunId("gate-stage2-run"),
            github_request=github_request,
            risk_class=RiskClass.LOW,
            requesting_principal_id="gate-agent",
        )
        workflow.verify_destinations(pypi_receipt, github_receipt)
    finally:
        ledger.close()

    if github_server.release_count != 1:
        raise AssertionError("expected exactly one GitHub release after the first publish")

    # Idempotent-replay proof: a second publish() call -- simulating a coordinator
    # restart after a lost response -- must not create a duplicate GitHub release or
    # a mismatched PyPI publish. The adapters are rebuilt with no memory of the
    # first call; only the fake destination servers (standing in for the real,
    # persistent external services) remember what already exists.
    ledger, workflow = reopen("full-system-gate-stage2-publish-replay")
    try:
        replay_pypi_receipt, replay_github_receipt = workflow.publish(
            artifacts,
            run_id=RunId("gate-stage2-run"),
            github_request=github_request,
            risk_class=RiskClass.LOW,
            requesting_principal_id="gate-agent",
        )
    finally:
        ledger.close()

    if github_server.release_count != 1:
        raise AssertionError(
            "replaying publish() after a simulated restart created a duplicate GitHub release"
        )
    if replay_github_receipt.artifact_digest != github_receipt.artifact_digest:
        raise AssertionError("replayed publish() did not report the same GitHub artifact digest")
    if replay_pypi_receipt.artifact_digest != pypi_receipt.artifact_digest:
        raise AssertionError("replayed publish() did not report the same PyPI artifact digest")

    return {
        "distribution_name": _STAGE2_DISTRIBUTION_NAME,
        "version": _STAGE2_VERSION,
        "merged_slices": list(pull_requests.merge_calls),
        "wheel_digest": str(artifacts.wheel.digest),
        "sdist_digest": str(artifacts.sdist.digest),
        "github_release_count": github_server.release_count,
        "pypi_published_versions": pypi_server.published_versions(),
        "restart_between_stages": restart_between_stages,
    }


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _check_stage3_reproduction(target: str) -> ReproductionRecord:
    body = json.dumps({"value": 2}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{target}/increment", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        observed = json.loads(response.read())["result"]
    if observed != 3:
        return ReproductionRecord(
            outcome=ReproductionOutcome.REPRODUCED,
            detail=f"increment(2) returned {observed}, expected 3",
        )
    return ReproductionRecord(
        outcome=ReproductionOutcome.UNAVAILABLE, detail="increment(2) returned the correct result"
    )


def _run_stage3_leg(
    database: Path, artifact_root: Path, *, restart_between_stages: bool
) -> dict[str, object]:
    """Drive the real ``IncidentService`` through ingest -> hotfix -> deploy ->
    observe -> roll back -> restore -> follow-up.

    See the module docstring for exactly what ``restart_between_stages``
    proves for this leg (ledger-backed incident-state persistence across a
    coordinator restart) and what it does not (policy/approval persistence,
    already covered by the Stage 2 leg's own documented caveat).
    """
    repo = artifact_root / "stage3-hotfix-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=repo)
    _git("config", "user.email", "full-system-gate@example.invalid", cwd=repo)
    _git("config", "user.name", "Full System Gate", cwd=repo)
    (repo / "app.py").write_text(_STAGE3_BUGGY_APP, encoding="utf-8")
    _git("add", "app.py", cwd=repo)
    _git("commit", "-m", "v1: buggy add()", cwd=repo)
    base_revision = _git("rev-parse", "HEAD", cwd=repo)

    adapter = LocalServiceDeploymentAdapter(
        artifacts_root=artifact_root / "stage3-artifacts",
        state_root=artifact_root / "stage3-state",
        app_script=_STAGE3_APP_SCRIPT,
        ready_attempts=50,
        ready_interval_seconds=0.05,
    )
    target = f"127.0.0.1:{_free_port()}"
    registry = ApprovalRegistry(registered_humans=(_STAGE3_HUMAN,))
    policy = PolicyEvaluator(policy_version="full-system-gate-1.0.0", approval_registry=registry)

    def reopen(owner: str) -> tuple[LedgerService, IncidentService]:
        ledger = LedgerService.open(database)
        return ledger, IncidentService(ledger=ledger, deployment=adapter, policy=policy)

    workspace = None
    ledger, service = reopen("full-system-gate-stage3-intake")
    try:
        v1_artifact = build_local_service_artifact(
            LocalServiceBuild(version=base_revision, defect_mode="increment_off_by_one"),
            artifacts_root=adapter.artifacts_root,
        )
        adapter.deploy(
            DeploymentRequest(
                run_id=RunId("full-system-gate-stage3"),
                artifact=v1_artifact,
                target=target,
                operation_id=OperationId(value="0" * 63 + "3"),
            )
        )
        incident = service.ingest(
            source_provider="full-system-gate",
            external_reference="stage3-leg-increment-bug",
            source_snapshot_reference=base_revision,
            title="checkout increment returns the wrong result",
            objective="restore correct checkout increment behavior",
            acceptance_criteria=("increment(2) returns 3",),
            repository_targets=("full-system-gate/stage3-fixture",),
            severity=IncidentSeverity.HIGH,
            summary="checkout increment endpoint is off by one",
        )
        incident_id = incident.id
        service.classify(incident_id)
        lineage = ReleaseLineage(service=target, affected_revision=base_revision)
        service.bind_release_lineage(incident_id, lineage)
        service.begin_reproduction(incident_id)
        reproduced = service.attempt_reproduction(
            incident_id, check=lambda: _check_stage3_reproduction(target)
        )
        if reproduced.state is not IncidentState.REMEDIATING:
            raise AssertionError("expected reproduction to confirm the incident")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen("full-system-gate-stage3-hotfix-check")
            if service.read(incident_id) is None:
                raise AssertionError("ingested incident did not survive a coordinator restart")

        worktree_root = artifact_root / "stage3-hotfix-workspace"
        workspace = create_hotfix_worktree(
            repository=repo,
            base_revision=base_revision,
            branch="hotfix/full-system-gate-increment",
            worktree_root=worktree_root,
        )
        repair = HotfixRepair(
            file_path="app.py", content=_STAGE3_FIXED_APP, commit_message="fix off-by-one in add()"
        )
        repaired_revision = apply_repair(workspace, repair)
        regression_evidence = prove_non_vacuous_regression(
            LocalValidation(),
            run_id=RunId("full-system-gate-stage3"),
            workspace=workspace,
            command=_STAGE3_CHECK_COMMAND,
            repaired_revision=repaired_revision,
        )
        if not regression_evidence.is_non_vacuous:
            raise AssertionError("regression evidence is vacuous; refusing to deploy")
        review = route_review(
            ReviewReport(
                producer="full-system-gate-worker", reviewer="independent-reviewer", findings=()
            ),
            repair_attempt=0,
            repair_limit=1,
        )
        if review is not ReviewOutcome.APPROVED:
            raise AssertionError("hotfix review did not approve")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen("full-system-gate-stage3-deploy")
        service.begin_deployment(incident_id)
        v2_artifact = build_local_service_artifact(
            LocalServiceBuild(version=repaired_revision, defect_mode="health_degraded"),
            artifacts_root=adapter.artifacts_root,
        )
        registry.record_approval(
            ApprovalSchema(
                action=PolicyAction.DEPLOYMENT_EXECUTE,
                risk_class=incident.risk_class,
                target_resource=target,
                diff_or_artifact_digest=str(v2_artifact.digest),
                requesting_principal_id=_STAGE3_REQUESTING_PRINCIPAL_ID,
            ),
            (_STAGE3_HUMAN,),
            decided_at=_NOW,
        )
        receipt = service.execute_deployment(
            incident_id,
            artifact=v2_artifact,
            requesting_principal_id=_STAGE3_REQUESTING_PRINCIPAL_ID,
            now=_NOW,
        )
        deployed = adapter.observe(target)
        if deployed.revision != repaired_revision:
            raise AssertionError("deployed revision does not match the hotfix revision")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen("full-system-gate-stage3-observe")
        service.begin_observation(incident_id)
        observation = adapter.observe(target, attempts=5, interval_seconds=0.05)
        if observation.healthy:
            raise AssertionError("expected the deployed revision to be unhealthy in this run")
        resolved = service.resolve_observation(incident_id, healthy=observation.healthy)
        if resolved.state is not IncidentState.ROLLING_BACK:
            raise AssertionError("unhealthy observation did not begin rollback")

        if restart_between_stages:
            ledger.close()
            ledger, service = reopen("full-system-gate-stage3-rollback")
        registry.record_approval(
            ApprovalSchema(
                action=PolicyAction.DEPLOYMENT_ROLLBACK,
                risk_class=incident.risk_class,
                target_resource=target,
                diff_or_artifact_digest=str(receipt.artifact_digest),
                requesting_principal_id=_STAGE3_REQUESTING_PRINCIPAL_ID,
            ),
            (_STAGE3_HUMAN,),
            decided_at=_NOW,
        )
        final_incident = service.execute_rollback(
            incident_id,
            receipt=receipt,
            requesting_principal_id=_STAGE3_REQUESTING_PRINCIPAL_ID,
            now=_NOW,
        )
        if final_incident.state is not IncidentState.ROLLED_BACK:
            raise AssertionError("rollback did not reach the rolled_back terminal state")
        restored = adapter.observe(target)
        if restored.revision != base_revision:
            raise AssertionError("rollback did not restore the prior revision")
        follow_up = service.record_follow_up(
            incident_id,
            title="investigate deployment health-check degradation on hotfix rollout",
            objective="root-cause why the hotfix build failed its deployment health check",
            acceptance_criteria=("root cause documented and a remediation plan filed",),
            repository_targets=("full-system-gate/stage3-fixture",),
        )
        authority_count = len(service.list_authority_records(incident_id))

        # Restart/idempotency proof: the terminal rolled_back state and its
        # authority-record count must be durably re-readable, not held only
        # in this process's local variables.
        if restart_between_stages:
            ledger.close()
            ledger, service = reopen("full-system-gate-stage3-reread")
        reread = service.read(incident_id)
        if reread is None or reread.state is not IncidentState.ROLLED_BACK:
            raise AssertionError("rolled_back terminal state is not durably re-readable")
        if len(service.list_authority_records(incident_id)) != authority_count:
            raise AssertionError("authority record count changed on re-read")

        return {
            "incident_id": str(incident_id),
            "base_revision": base_revision,
            "repaired_revision": repaired_revision,
            "regression_non_vacuous": regression_evidence.is_non_vacuous,
            "deployed_revision": repaired_revision,
            "restored_revision": restored.revision,
            "authority_record_count": authority_count,
            "follow_up_work_item_id": str(follow_up.id),
            "final_state": final_incident.state.value,
            "restart_between_stages": restart_between_stages,
        }
    finally:
        ledger.close()
        state = adapter._read_state(target)
        if state is not None:
            adapter._stop(state["current"])
            if state.get("previous") is not None:
                adapter._stop(state["previous"])
        if workspace is not None and workspace.root.exists():
            remove_hotfix_worktree(repository=repo, workspace=workspace)


@dataclass(slots=True)
class GateReport:
    stages: str
    restart_between_stages: bool
    run_evidence: list[dict[str, object]] = field(default_factory=list)
    stage2_evidence: dict[str, object] | None = None
    stage3_evidence: dict[str, object] | None = None
    evidence_digest: str = ""


def run_gate(
    *, stages: str = "1", restart_between_stages: bool = True, run_count: int = 2
) -> GateReport:
    requested_stages = frozenset(part.strip() for part in stages.split(",") if part.strip())
    if not requested_stages or not requested_stages <= _SUPPORTED_STAGES:
        raise EngineryError(
            "full_system_gate.py only supports --stages combinations of 1, 2, and 3; "
            "Stage 4 belongs to its own gate-deferred release train once gate G4 passes",
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
        if "1" in requested_stages:
            database = artifact_root / "stage1-ledger.db"
            for run_index in range(1, run_count + 1):
                fixture = _build_fixture(run_index, artifact_root)
                evidence = _drive_to_terminal(
                    database, fixture, restart_between_stages=restart_between_stages
                )
                report.run_evidence.append(evidence)
        if "2" in requested_stages:
            stage2_database = artifact_root / "stage2-ledger.db"
            stage2_fixture_root = artifact_root / "stage2-fixture"
            report.stage2_evidence = _run_stage2_leg(
                stage2_database, stage2_fixture_root, restart_between_stages=restart_between_stages
            )
        if "3" in requested_stages:
            stage3_database = artifact_root / "stage3-ledger.db"
            report.stage3_evidence = _run_stage3_leg(
                stage3_database, artifact_root, restart_between_stages=restart_between_stages
            )
    report.evidence_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "stage1": report.run_evidence,
                    "stage2": report.stage2_evidence,
                    "stage3": report.stage3_evidence,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cumulative Stage-1/Stage-2/Stage-3 restart/replay gate (local fixtures only)."
    )
    parser.add_argument(
        "--stages",
        default="1",
        help="comma-separated stages to gate; any combination of '1', '2', '3'",
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
        f"stage1_runs={len(report.run_evidence)} "
        f"stage2_evidence={'yes' if report.stage2_evidence is not None else 'no'} "
        f"stage3_evidence={'yes' if report.stage3_evidence is not None else 'no'} "
        f"evidence_digest={report.evidence_digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
