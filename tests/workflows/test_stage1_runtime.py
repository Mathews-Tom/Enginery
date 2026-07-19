from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from enginery.application.work_ports import WorkLedgerPort, WorkLedgerSnapshot
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.ids import WorkItemId
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import IssueReadiness, issue_to_pr_manifest
from enginery.workflows.stage1_runtime import Stage1QualificationExecutor


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _request(tmp_path: Path) -> FixtureDispatch:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return FixtureDispatch(
        run_id="run-1",
        node_id="qualify",
        attempt_id="attempt-1",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace",
        base_revision=_git("rev-parse", "HEAD", cwd=repository),
        command=("unused",),
        expected_attempt_version=0,
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )


def _snapshot() -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("work-1"),
            work_kind=WorkKind.ISSUE,
            source_provider="github",
            external_reference="issue:1",
            source_snapshot_reference="issue:1@1",
            title="Bounded change",
            objective="Change one bounded behavior.",
            acceptance_criteria=("observable result",),
            constraints=("retain evidence",),
            risk_class=RiskClass.LOW,
            repository_targets=("repository-1",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="1",
    )


class RecordingWorkLedger:
    def __init__(self, ledger: LedgerService, snapshot: WorkLedgerSnapshot) -> None:
        self.ledger = ledger
        self.snapshot = snapshot

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        node = self.ledger.read_projection(
            aggregate_type="runtime_node", aggregate_id="run-1:qualify"
        )
        assert external_reference == "issue:1"
        assert node is not None
        assert node.state["status"] == "queued"
        return self.snapshot


def test_qualification_persists_manifest_node_before_provider_fetch(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    executor = Stage1QualificationExecutor(
        runtime, cast(WorkLedgerPort, RecordingWorkLedger(ledger_service, _snapshot()))
    )

    qualification = executor.qualify(
        dispatch=WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest()),
        external_reference="issue:1",
        applicable_criteria=(True,),
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )

    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:qualify"
    )
    assert qualification.readiness is IssueReadiness.READY
    assert node is not None
    assert node.state["status"] == "passed"
    assert node.state["source_revision"] == "1"


def test_tick_does_not_dispatch_a_recovered_deterministic_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())

    runtime.register_node(dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60))
    tick = runtime.tick(
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
    )

    assert tick.dispatched == ()


def test_manifest_node_dispatch_rejects_agent_nodes(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="non-agent"):
        WorkflowNodeDispatch(
            replace(
                _request(tmp_path),
                node_id="implement",
                dependencies=(("run-1", "qualify"),),
            ),
            issue_to_pr_manifest(),
        )


def test_manifest_registration_renews_its_active_epoch(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())

    first = runtime.register_node(
        dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60)
    )
    second = runtime.register_node(
        dispatch=dispatch,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
    )

    assert second.epoch == first.epoch


def test_manifest_registration_requires_completed_dependencies(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(
        replace(
            _request(tmp_path),
            node_id="validate",
            dependencies=(("run-1", "implement"),),
        ),
        issue_to_pr_manifest(),
    )

    with pytest.raises(ExternalConflictError, match="dependencies"):
        runtime.register_node(dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60))


def test_manifest_node_dispatch_rejects_dependency_bypass(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="dependencies"):
        WorkflowNodeDispatch(
            replace(_request(tmp_path), dependencies=(("run-1", "unrelated"),)),
            issue_to_pr_manifest(),
        )


def test_raw_worker_dispatch_cannot_replace_a_manifest_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    runtime.register_node(dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60))

    with pytest.raises(ExternalConflictError, match="actor type"):
        runtime.tick(
            now=now + timedelta(seconds=1),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            requests=(dispatch.request,),
        )


def test_human_wait_resolution_requires_prior_qualification(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    qualification = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=qualification, now=now, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=now)
    approval = WorkflowNodeDispatch(
        replace(
            qualification.request,
            node_id="plan_approval",
            attempt_id="attempt-approval",
            operation_id="operation-approval",
            dependencies=(("run-1", "qualify"),),
        ),
        issue_to_pr_manifest(),
    )

    runtime.register_node(dispatch=approval, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.await_human_node(
        run_id="run-1",
        node_id="plan_approval",
        epoch=epoch.epoch,
        now=now,
        reason="approval required",
    )
    runtime.resolve_human_wait(
        run_id="run-1",
        node_id="plan_approval",
        epoch=epoch.epoch,
        now=now,
        outcome="passed",
        extra={"decision": "approved"},
    )

    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:plan_approval"
    )
    assert projection is not None
    assert projection.state["status"] == "passed"
    assert projection.state["decision"] == "approved"


def test_terminal_node_retry_requires_new_fenced_identity(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    initial = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(dispatch=initial, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=now)
    retry = replace(
        initial.request,
        attempt_id="attempt-2",
        operation_id="operation-2",
    )

    runtime.retry_node(
        request=retry,
        actor_type=ActorType.DETERMINISTIC,
        epoch=epoch.epoch,
        now=now,
    )

    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:qualify"
    )
    assert projection is not None
    assert projection.state["status"] == "queued"
    assert projection.state["attempt_id"] == "attempt-2"


def test_human_wait_resolution_rejects_nonwaiting_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60)
    )

    with pytest.raises(ExternalConflictError, match="human-waiting"):
        runtime.resolve_human_wait(
            run_id="run-1",
            node_id="qualify",
            epoch=epoch.epoch,
            now=now,
            outcome="passed",
        )


def test_terminal_node_retry_rejects_reused_or_redefined_request(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    initial = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(dispatch=initial, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=now)

    with pytest.raises(InvalidInputError, match="fresh attempt"):
        runtime.retry_node(
            request=replace(initial.request, operation_id="operation-2"),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )
    with pytest.raises(InvalidInputError, match="fresh attempt"):
        runtime.retry_node(
            request=replace(initial.request, attempt_id="attempt-2"),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )
    with pytest.raises(ExternalConflictError, match="actor type"):
        runtime.retry_node(
            request=replace(
                initial.request,
                attempt_id="attempt-2",
                operation_id="operation-2",
            ),
            actor_type=ActorType.HUMAN,
            epoch=epoch.epoch,
            now=now,
        )
    with pytest.raises(ExternalConflictError, match="immutable"):
        runtime.retry_node(
            request=replace(
                initial.request,
                attempt_id="attempt-2",
                operation_id="operation-2",
                base_revision="different-base",
            ),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )
