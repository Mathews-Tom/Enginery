from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.stage1 import (
    Stage1ImplementationRequest,
    Stage1RunRequest,
    stage1_request_from_state,
)


def test_stage1_start_watch_and_evidence_are_ledger_backed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request(tmp_path).initial_state()), encoding="utf-8")

    assert _start(database, request_path) == 0
    assert json.loads(capsys.readouterr().out) == {"run_id": "run-stage1", "status": "created"}

    assert _lifecycle(database, "watch", "--run-id", "run-stage1") == 0
    watched = json.loads(capsys.readouterr().out)
    assert watched["run_id"] == "run-stage1"
    assert watched["nodes"] == []

    assert _lifecycle(database, "evidence", "--run-id", "run-stage1") == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["request_digest"] == str(_request(tmp_path).digest)
    assert evidence["source_revision"] == "issue-revision-1"


def test_stage1_restart_is_idempotent_and_rejects_changed_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _request(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request.initial_state()), encoding="utf-8")

    assert _start(database, request_path) == 0
    capsys.readouterr()
    assert _start(database, request_path) == 0
    assert json.loads(capsys.readouterr().out) == {"run_id": "run-stage1", "status": "created"}

    changed_request = replace(
        request,
        validation_commands=(("uv", "run", "pytest", "tests/cli/test_stage1.py", "-q"),),
    )
    request_path.write_text(
        json.dumps(changed_request.initial_state()),
        encoding="utf-8",
    )
    assert _start(database, request_path) != 0
    assert "different immutable request" in capsys.readouterr().err


@pytest.mark.parametrize("cost_budget", ("not-a-decimal", "NaN", "Infinity"))
def test_stage1_rejects_invalid_persisted_implementation_budget(
    tmp_path: Path, cost_budget: str
) -> None:
    state = _request(tmp_path).initial_state()
    implementation = state["implementation"]
    assert isinstance(implementation, dict)
    implementation["cost_budget"] = cost_budget

    with pytest.raises(InvalidInputError, match="cost"):
        stage1_request_from_state(state)


def test_stage1_approve_resume_and_cancel_route_through_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _start_request(database, tmp_path)
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="plan_approval")

    assert (
        _lifecycle(
            database,
            "resume",
            "--run-id",
            "run-stage1",
            "--node-id",
            "plan_approval",
            "--attempt-id",
            "plan-approval-1",
            "--operation-id",
            "plan-approval:run-stage1:1",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "queued"

    assert (
        _lifecycle(
            database,
            "cancel",
            "--run-id",
            "run-stage1",
            "--node-id",
            "plan_approval",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"

    _register_human_node(database, request, node_id="no_change_confirmation")
    assert (
        _lifecycle(
            database,
            "approve",
            "--run-id",
            "run-stage1",
            "--node-id",
            "no_change_confirmation",
            "--reason",
            "operator confirmed non-applicability",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_stage1_cancel_rejects_human_wait_without_worker_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _start_request(database, tmp_path)
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="plan_approval")

    assert (
        _lifecycle(
            database,
            "cancel",
            "--run-id",
            "run-stage1",
            "--node-id",
            "plan_approval",
        )
        != 0
    )
    assert "cannot cancel a human-waiting non-agent node" in capsys.readouterr().err


def test_stage1_rejects_human_wait_and_invalid_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = replace(
        _request(tmp_path),
        run=replace(_request(tmp_path).run, id=RunId("run-stage1-reject")),
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request.initial_state()), encoding="utf-8")
    assert _start(database, request_path) == 0
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="plan_approval")

    assert (
        _lifecycle(
            database,
            "reject",
            "--run-id",
            "run-stage1-reject",
            "--node-id",
            "plan_approval",
            "--reason",
            "operator rejected the proposed plan",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    invalid_request = tmp_path / "invalid-request.json"
    invalid_request.write_bytes(b"\xff")
    assert _start(database, invalid_request) != 0
    assert "UTF-8" in capsys.readouterr().err


def _start_request(database: Path, tmp_path: Path) -> Stage1RunRequest:
    request = _request(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request.initial_state()), encoding="utf-8")
    assert _start(database, request_path) == 0
    return request


def _start(database: Path, request_path: Path) -> int:
    return main(
        [
            "stage1",
            "start",
            "--database",
            str(database),
            "--owner",
            "stage1-cli",
            "--request",
            str(request_path),
        ]
    )


def _lifecycle(database: Path, command: str, *arguments: str) -> int:
    return main(
        ["stage1", command, "--database", str(database), "--owner", "stage1-cli", *arguments]
    )


def _register_qualify_and_human_node(
    database: Path, request: Stage1RunRequest, *, node_id: str
) -> None:
    ledger = LedgerService.open(database)
    try:
        runtime = CoordinatorRuntime(ledger, owner="stage1-cli")
        now = datetime.now(tz=UTC)
        qualify = _dispatch(request, node_id="qualify", dependencies=())
        epoch = runtime.register_node(
            dispatch=WorkflowNodeDispatch(qualify, request.manifest),
            now=now,
            heartbeat_window=timedelta(seconds=60),
        )
        runtime.complete_node(
            run_id=str(request.run.id),
            node_id="qualify",
            epoch=epoch.epoch,
            now=now,
        )
        _register_human_node_with_runtime(runtime, request, node_id=node_id, now=now)
    finally:
        ledger.close()


def _register_human_node(database: Path, request: Stage1RunRequest, *, node_id: str) -> None:
    ledger = LedgerService.open(database)
    try:
        _register_human_node_with_runtime(
            CoordinatorRuntime(ledger, owner="stage1-cli"),
            request,
            node_id=node_id,
            now=datetime.now(tz=UTC),
        )
    finally:
        ledger.close()


def _register_human_node_with_runtime(
    runtime: CoordinatorRuntime,
    request: Stage1RunRequest,
    *,
    node_id: str,
    now: datetime,
) -> None:
    dispatch = _dispatch(request, node_id=node_id, dependencies=("qualify",))
    epoch = runtime.register_node(
        dispatch=WorkflowNodeDispatch(dispatch, request.manifest),
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )
    runtime.await_human_node(
        run_id=str(request.run.id),
        node_id=node_id,
        epoch=epoch.epoch,
        now=now,
        reason="operator decision required",
    )


def _dispatch(
    request: Stage1RunRequest, *, node_id: str, dependencies: tuple[str, ...]
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id=str(request.run.id),
        node_id=node_id,
        attempt_id=f"{node_id}-0",
        repository_id=request.repository_id,
        repository_path=request.repository_path,
        workspace_path=request.workspace_path,
        base_revision=request.run.base_revision,
        command=("fixture",),
        expected_attempt_version=0,
        operation_id=f"{node_id}:{request.run.id}:0",
        dependencies=tuple((str(request.run.id), dependency) for dependency in dependencies),
        workflow_definition_id=request.manifest.id.value,
    )


def _request(tmp_path: Path) -> Stage1RunRequest:
    manifest = issue_to_pr_manifest()
    work_item = WorkItem(
        id=WorkItemId("work-1"),
        work_kind=WorkKind.ISSUE,
        source_provider="github",
        external_reference="Mathews-Tom/Enginery#1",
        source_snapshot_reference="issue:1@issue-revision-1",
        title="Bounded change",
        objective="Change one bounded behavior.",
        acceptance_criteria=("observable result",),
        constraints=("retain evidence",),
        risk_class=RiskClass.LOW,
        repository_targets=("Mathews-Tom/Enginery",),
        dependencies=(),
        state=WorkItemState.QUALIFYING,
    )
    snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision="issue-revision-1")
    return Stage1RunRequest(
        run=Run(
            id=RunId("run-stage1"),
            work_item_id=work_item.id,
            work_item_snapshot_digest=work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="Mathews-Tom/Enginery",
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
        repository_id="Mathews-Tom/Enginery",
        repository_path=tmp_path.resolve(),
        workspace_path=(tmp_path / "workspace").resolve(),
        base_branch="main",
        head_branch="enginery/stage1",
        validation_commands=(("uv", "run", "pytest", "-q"),),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:run-stage1"),
            time_budget_seconds=60,
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
    )
