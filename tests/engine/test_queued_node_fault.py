"""Regression for the pilot-identified "queued but not selected" liveness defect.

An implementation node registered while an unrelated run already occupies the sole global
scheduling slot is not selected on its registering tick. It must stay a durable, visible
queued state -- not an error -- and the first coordinator evaluation after the capacity is
released must dispatch it exactly once. These tests drive the real ``Stage1RunService``
composition against a real SQLite ledger and real worker subprocesses.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.adapter_types import AdapterStatus
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import LifecycleProjection, WorkLedgerSnapshot
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import HumanActionRequiredError
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1ProgressionAction,
    Stage1RunRequest,
    Stage1RunService,
)
from tests.support.execution import selection_for

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
    repository, base_revision = _repository(tmp_path, "repository-under-test")
    manifest = stage1_work_manifest()
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
            base_revision=base_revision,
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
        execution_selection=selection_for("omp"),
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


def _sleeping_harness(tmp_path: Path) -> str:
    """A real executable that stays alive until its process group is terminated."""
    script = tmp_path / "harness-sleep"
    script.write_text("#!/bin/sh\nexec sleep 120\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _service(
    ledger: LedgerService, runtime: CoordinatorRuntime, request: Stage1RunRequest, tmp_path: Path
) -> Stage1RunService:
    return Stage1RunService(
        runtime=runtime,
        ledger=ledger,
        work_ledger=_FixedWorkLedger(request.work_snapshot),
        harness=OmpHarness(
            OmpAdapterConfig(
                credential_reference="unused-fixture", executable=_sleeping_harness(tmp_path)
            ),
            ArtifactStore(request.execution_configuration.artifact_root),
        ),
    )


def _event_count(ledger: LedgerService, *, event_type: str, aggregate_id: str) -> int:
    row = ledger.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND aggregate_id = ?",
        (event_type, aggregate_id),
    ).fetchone()
    return int(row[0])


def _implement_state(ledger: LedgerService) -> dict[str, object]:
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id="run-fault:implement")
    assert node is not None
    return dict(node.state)


def _queue_implement_behind_occupier(
    ledger: LedgerService, tmp_path: Path, now: datetime
) -> tuple[CoordinatorRuntime, Stage1RunService, Stage1RunRequest, tuple[str, str]]:
    runtime = CoordinatorRuntime(ledger, owner="operator")
    request = _qualified_request(tmp_path)
    service = _service(ledger, runtime, request, tmp_path)
    occupier = _occupy_global_capacity(runtime, tmp_path=tmp_path, now=now)
    service.start(request, now=now, heartbeat_window=timedelta(seconds=60))
    service.qualify(
        request,
        external_reference=request.work_snapshot.work_item.external_reference,
        applicable_criteria=request.applicable_criteria,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
    )
    assert service.next_action(request.run.id).action.value == "implement"
    return runtime, service, request, occupier


def _release_occupier(
    runtime: CoordinatorRuntime, occupier: tuple[str, str], now: datetime
) -> None:
    epoch = runtime.claim_epoch(now=now, heartbeat_window=timedelta(seconds=60))
    runtime.cancel_node(
        run_id=occupier[0],
        node_id=occupier[1],
        epoch=epoch.epoch,
        now=now + timedelta(seconds=1),
    )


def _advance(
    service: Stage1RunService, request: Stage1RunRequest, now: datetime
) -> Stage1ProgressionAction:
    return service.advance(
        request.run.id,
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
    ).action


def test_queued_implement_redispatches_exactly_once_after_capacity_is_released(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    runtime, service, request, occupier = _queue_implement_behind_occupier(
        ledger_service, tmp_path, now
    )

    # The registering tick cannot select the node: it is a visible queued state, not an error.
    registered = service.dispatch_implementation(
        request,
        now=now + timedelta(seconds=2),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
    )
    assert registered.fixture is None
    assert registered.wakeup is not None
    assert registered.wakeup["reason"] == "global_capacity"
    assert registered.wakeup["evaluations"] == 1
    assert _implement_state(ledger_service)["status"] == "queued"
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.DISPATCH_QUEUED

    # While the slot is still occupied, advancing keeps it queued, records the evaluation and
    # launches nothing.
    assert (
        _advance(service, request, now + timedelta(seconds=3))
        is Stage1ProgressionAction.DISPATCH_QUEUED
    )
    still_queued = _implement_state(ledger_service)
    assert still_queued["status"] == "queued"
    wakeup = still_queued["wakeup"]
    assert isinstance(wakeup, dict)
    assert wakeup["reason"] == "global_capacity"
    assert wakeup["evaluations"] == 2
    assert ledger_service.read_lease(run_id="run-fault", node_id="implement") is None
    assert (
        _event_count(
            ledger_service, event_type="worker.launch_intended", aggregate_id="run-fault:implement"
        )
        == 0
    )

    # Releasing the occupying slot for real makes the next evaluation dispatch the same
    # operation exactly once.
    _release_occupier(runtime, occupier, now + timedelta(seconds=5))
    assert _advance(service, request, now + timedelta(seconds=10)) is Stage1ProgressionAction.WAIT
    running = _implement_state(ledger_service)
    assert running["status"] == "running"
    assert running["operation_id"] == str(request.implementation.operation_id)
    closed = running["wakeup"]
    assert isinstance(closed, dict)
    assert closed["reason"] is None
    assert closed["queued_since"] == (now + timedelta(seconds=2)).isoformat()
    assert closed["dispatched_at"] == (now + timedelta(seconds=10)).isoformat()
    lease = ledger_service.read_lease(run_id="run-fault", node_id="implement")
    assert lease is not None

    # Repeated evaluation never duplicates the lease, worker or launch.
    for offset in (11, 12, 13):
        assert _advance(service, request, now + timedelta(seconds=offset)) is (
            Stage1ProgressionAction.WAIT
        )
    assert ledger_service.read_lease(run_id="run-fault", node_id="implement") == lease
    for event_type in ("worker.launch_intended", "worker.started"):
        assert (
            _event_count(ledger_service, event_type=event_type, aggregate_id="run-fault:implement")
            == 1
        )

    # Tear down the real worker process group.
    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=20), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id="run-fault",
        node_id="implement",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=21),
    )


def test_queued_implement_is_cancellable_and_reaches_human_review(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    runtime, service, request, _ = _queue_implement_behind_occupier(ledger_service, tmp_path, now)
    service.dispatch_implementation(
        request,
        now=now + timedelta(seconds=2),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
    )

    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=5), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id="run-fault",
        node_id="implement",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=6),
    )

    assert _implement_state(ledger_service)["status"] == "cancelled"
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
    assert _advance(service, request, now + timedelta(seconds=7)) is (
        Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
    )


def test_queued_implement_is_held_not_dispatched_while_the_catalog_has_drifted(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    runtime, service, request, occupier = _queue_implement_behind_occupier(
        ledger_service, tmp_path, now
    )
    service.dispatch_implementation(
        request,
        now=now + timedelta(seconds=2),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
    )
    _release_occupier(runtime, occupier, now + timedelta(seconds=5))
    drifted = replace(
        service,
        execution_catalog=replace(
            load_execution_catalog(), digest=Digest.of_bytes(b"a later catalog"), version="2"
        ),
    )

    # Capacity is free, but the drifted binding holds the node: nothing may launch.
    assert drifted.next_action(request.run.id).action is Stage1ProgressionAction.HOLD
    assert _advance(drifted, request, now + timedelta(seconds=10)) is Stage1ProgressionAction.HOLD
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.redispatch_queued_implementation(
            request,
            now=now + timedelta(seconds=11),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=_LIMITS,
        )
    assert _implement_state(ledger_service)["status"] == "queued"
    assert ledger_service.read_lease(run_id="run-fault", node_id="implement") is None

    # Once the binding is current again the same queued node dispatches.
    assert _advance(service, request, now + timedelta(seconds=12)) is Stage1ProgressionAction.WAIT
    assert _implement_state(ledger_service)["status"] == "running"
    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=20), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id="run-fault",
        node_id="implement",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=21),
    )


def test_an_older_queued_run_keeps_its_priority_and_the_newer_run_records_the_reservation(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    runtime, service, request, occupier = _queue_implement_behind_occupier(
        ledger_service, tmp_path, now
    )
    repository, base_revision = _repository(tmp_path, "foreign-repository")
    foreign = FixtureDispatch(
        run_id="run-aaa-foreign",
        node_id="work",
        attempt_id="attempt-foreign",
        repository_id="repository-foreign",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-foreign",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-foreign",
    )
    # Queued first, and first in fair order: it must win the freed slot.
    assert (
        runtime.tick(
            now=now + timedelta(seconds=1),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=_LIMITS,
            requests=(foreign,),
        ).dispatched
        == ()
    )
    service.dispatch_implementation(
        request,
        now=now + timedelta(seconds=2),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
    )
    _release_occupier(runtime, occupier, now + timedelta(seconds=5))

    # The newer run's advance cannot take the slot the fair plan granted to the older run,
    # and it never launches the older run's node itself.
    assert (
        _advance(service, request, now + timedelta(seconds=10))
        is Stage1ProgressionAction.DISPATCH_QUEUED
    )
    waiting = _implement_state(ledger_service)
    assert waiting["status"] == "queued"
    wakeup = waiting["wakeup"]
    assert isinstance(wakeup, dict)
    assert wakeup["reason"] == "capacity_reserved_for_other_run"
    assert wakeup["reserved_for"] == "run-aaa-foreign"
    assert wakeup["evaluations"] == 2
    assert ledger_service.read_lease(run_id="run-fault", node_id="implement") is None
    foreign_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-aaa-foreign:work"
    )
    assert foreign_node is not None
    assert foreign_node.state["status"] == "queued"
    assert ledger_service.read_lease(run_id="run-aaa-foreign", node_id="work") is None

    # The older run launches on its own advance; the newer one then waits for real capacity.
    started = runtime.tick(
        now=now + timedelta(seconds=11),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
        run_ids=("run-aaa-foreign",),
    ).dispatched
    assert [fixture.lease.run_id for fixture in started] == ["run-aaa-foreign"]
    assert (
        _advance(service, request, now + timedelta(seconds=12))
        is Stage1ProgressionAction.DISPATCH_QUEUED
    )
    blocked = _implement_state(ledger_service)["wakeup"]
    assert isinstance(blocked, dict)
    assert blocked["reason"] == "global_capacity"
    assert blocked["reserved_for"] is None

    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=13), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id="run-aaa-foreign",
        node_id="work",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=14),
    )
    assert _advance(service, request, now + timedelta(seconds=15)) is Stage1ProgressionAction.WAIT
    assert _implement_state(ledger_service)["status"] == "running"
    runtime.cancel_node(
        run_id="run-fault",
        node_id="implement",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=16),
    )


def test_a_held_older_run_does_not_strand_capacity_a_current_newer_run_can_use(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    runtime, service, request, occupier = _queue_implement_behind_occupier(
        ledger_service, tmp_path, now
    )
    service.dispatch_implementation(
        request,
        now=now + timedelta(seconds=2),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
    )
    repository, base_revision = _repository(tmp_path, "newer-repository")
    newer = FixtureDispatch(
        run_id="run-newer",
        node_id="work",
        attempt_id="attempt-newer",
        repository_id="repository-newer",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-newer",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-newer",
    )
    assert (
        runtime.tick(
            now=now + timedelta(seconds=3),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=_LIMITS,
            requests=(newer,),
            run_ids=("run-newer",),
        ).dispatched
        == ()
    )
    _release_occupier(runtime, occupier, now + timedelta(seconds=5))
    drifted = replace(
        service,
        execution_catalog=replace(
            load_execution_catalog(), digest=Digest.of_bytes(b"a later catalog"), version="2"
        ),
    )

    # The older run's binding is held, so the fair plan skips it and the newer run launches.
    held = drifted._held_runs()
    assert list(held) == ["run-fault"]
    started = runtime.tick(
        now=now + timedelta(seconds=6),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=_LIMITS,
        run_ids=("run-newer",),
        held_runs=held,
    ).dispatched
    assert [fixture.lease.run_id for fixture in started] == ["run-newer"]
    waiting = _implement_state(ledger_service)
    assert waiting["status"] == "queued"
    wakeup = waiting["wakeup"]
    assert isinstance(wakeup, dict)
    assert wakeup["reason"] == "execution_hold"
    assert wakeup["reserved_for"] is None
    assert "catalog changed" in str(wakeup["detail"])
    assert ledger_service.read_lease(run_id="run-fault", node_id="implement") is None

    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=7), heartbeat_window=timedelta(seconds=60)
    )
    runtime.cancel_node(
        run_id="run-newer", node_id="work", epoch=epoch.epoch, now=now + timedelta(seconds=8)
    )
    assert _advance(service, request, now + timedelta(seconds=9)) is Stage1ProgressionAction.WAIT
    runtime.cancel_node(
        run_id="run-fault", node_id="implement", epoch=epoch.epoch, now=now + timedelta(seconds=10)
    )
