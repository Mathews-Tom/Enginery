"""Fault injection for the pilot-identified "queued but not selected" gap.

``docs/pitch.md``'s "Operator burden" finding: "a node that reaches
``queued`` but is not selected within its registering tick has no
automatic retry path, only an explicit ``stage1 cancel``." This test
reproduces the exact scenario -- an unrelated run already occupying the
sole global scheduling slot when ``dispatch_implementation`` ticks --
and records the observed behavior exactly, rather than inventing a fix
the milestone's own scope forbids assuming is needed.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.adapter_types import AdapterStatus
from enginery.application.work_ports import LifecycleProjection, WorkLedgerSnapshot
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import ExternalConflictError
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
)

_LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path, name: str) -> tuple[Path, str]:
    repository = tmp_path / name
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


class _FixedWorkLedger:
    """A minimal WorkLedgerPort double returning one fixed snapshot.

    Only ``fetch`` is exercised by this test's call sequence
    (``Stage1QualificationExecutor.qualify``); the other three protocol
    methods raise loudly if this scenario ever reaches them.
    """

    def __init__(self, snapshot: WorkLedgerSnapshot) -> None:
        self.snapshot = snapshot

    def probe(self) -> AdapterStatus:
        raise NotImplementedError("not exercised by this fault-injection scenario")

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        assert external_reference == self.snapshot.work_item.external_reference
        return self.snapshot

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        raise NotImplementedError("not exercised by this fault-injection scenario")

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        raise NotImplementedError("not exercised by this fault-injection scenario")


def _qualified_request(tmp_path: Path) -> Stage1RunRequest:
    repository, _base_revision = _repository(tmp_path, "repository-under-test")
    manifest = issue_to_pr_manifest()
    work_item = WorkItem(
        id=WorkItemId("work-fault"),
        work_kind=WorkKind.ISSUE,
        source_provider="github",
        external_reference="owner/repo#fault",
        source_snapshot_reference="issue:fault@1",
        title="Bounded fault-injection change",
        objective="Reproduce a queued-but-not-selected implement node.",
        acceptance_criteria=("observable result",),
        constraints=(),
        risk_class=RiskClass.LOW,
        repository_targets=("repository-fault",),
        dependencies=(),
        state=WorkItemState.QUALIFYING,
    )
    snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision="1")
    return Stage1RunRequest(
        run=Run(
            id=RunId("run-fault"),
            work_item_id=work_item.id,
            work_item_snapshot_digest=work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="repository-fault",
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
        repository_id="repository-fault",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-fault",
        base_branch="main",
        head_branch="enginery/run-fault",
        validation_commands=(("true",),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:run-fault"),
            time_budget_seconds=60,
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="owner/repo",
            github_credential_reference="unused-fixture",
            github_executable="true",
            harness_provider="omp",
            harness_credential_reference="unused-fixture",
            harness_executable="true",
            artifact_root=tmp_path / "artifacts-fault",
        ),
    )


def _occupy_global_capacity(
    runtime: CoordinatorRuntime, *, tmp_path: Path, now: datetime
) -> tuple[str, str]:
    """Fill the sole global concurrency slot with an unrelated, running node --
    the exact condition under which a freshly-registered node can be queued but
    not selected on its registering tick. Returns (run_id, node_id) so the
    caller can later free the slot for real."""
    repository, base_revision = _repository(tmp_path, "occupier-repository")
    occupier = FixtureDispatch(
        run_id="run-occupier",
        node_id="hold",
        attempt_id="attempt-1",
        repository_id="repository-occupier",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-occupier",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-occupier",
    )
    tick = runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
        requests=(occupier,),
    )
    assert len(tick.dispatched) == 1, "the occupier itself must be scheduled to consume capacity"
    return occupier.run_id, occupier.node_id


def test_queued_implement_node_has_no_automatic_retry_and_cancel_recovers_it(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    request = _qualified_request(tmp_path)
    service = Stage1RunService(
        runtime=runtime,
        ledger=ledger_service,
        work_ledger=_FixedWorkLedger(request.work_snapshot),
        harness=OmpHarness(
            OmpAdapterConfig(credential_reference="unused-fixture", executable="true"),
            ArtifactStore(request.execution_configuration.artifact_root),
        ),
    )

    # Occupy the sole global concurrency slot with an unrelated run before the
    # request under test ever registers its implement node.
    occupier_run_id, occupier_node_id = _occupy_global_capacity(runtime, tmp_path=tmp_path, now=now)

    service.start(request, now=now, heartbeat_window=timedelta(seconds=60))
    service.qualify(
        request,
        external_reference=request.work_snapshot.work_item.external_reference,
        applicable_criteria=request.applicable_criteria,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
    )
    assert service.next_action(request.run.id).action.value == "implement"

    # dispatch_implementation registers the "implement" node as queued and ticks
    # in the same call; global capacity is already exhausted, so it is never
    # selected -- reproducing the pilot's exact finding.
    with pytest.raises(ExternalConflictError, match="was not scheduled"):
        service.dispatch_implementation(
            request,
            now=now + timedelta(seconds=2),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=_LIMITS,
        )

    implement_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-fault:implement"
    )
    assert implement_node is not None
    assert implement_node.state["status"] == "queued"

    # The node is durably queued but next_action never reports "implement" again
    # (it is not None any more), so nothing re-attempts scheduling it.
    assert service.next_action(request.run.id).action.value == "wait"

    # Genuinely free the occupied slot by cancelling the occupier -- if any
    # automatic retry existed, it would now have room to run.
    free_epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=5), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id=occupier_run_id,
        node_id=occupier_node_id,
        epoch=free_epoch.epoch,
        now=now + timedelta(seconds=6),
    )

    # Even with capacity now genuinely free, repeated advance() calls stay stuck
    # at "wait": advance()'s action table has no handler for WAIT, so nothing
    # ever re-ticks the stuck node. This is the confirmed, reproducible gap.
    for offset in (10, 20, 30):
        progression = service.advance(
            request.run.id,
            now=now + timedelta(seconds=offset),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=_LIMITS,
        )
        assert progression.action.value == "wait"

    # The accepted recovery path is exactly what docs/pitch.md and
    # docs/operations.md already document: stage1 cancel. Claim a fresh epoch
    # (matching the CLI's own _cancel) and cancel the stuck queued node.
    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=40), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id=str(request.run.id),
        node_id="implement",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=41),
    )

    cancelled_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-fault:implement"
    )
    assert cancelled_node is not None
    assert cancelled_node.state["status"] == "cancelled"

    # Recovery is real and reachable: next_action now routes to the existing,
    # already-documented human-review/repair path -- no dedicated stuck-recovery
    # feature is needed beyond what stage1 cancel + stage1 review already do.
    assert service.next_action(request.run.id).action.value == "await_human_review"
