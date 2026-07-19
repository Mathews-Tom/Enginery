from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _request(
    *,
    repository: Path,
    revision: str,
    workspace: Path,
    attempt_id: str,
    operation_id: str,
    workflow_definition_id: str | None = None,
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id="run-1",
        node_id="node-1",
        attempt_id=attempt_id,
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=workspace,
        base_revision=revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id=operation_id,
        workflow_definition_id=workflow_definition_id,
    )


def _manifest(*, agent_node: bool = True) -> WorkflowManifest:
    node_kind = "execute_agent_task" if agent_node else "normalize_work"
    actor_type = "agent" if agent_node else "deterministic"
    return WorkflowManifest.from_mapping(
        {
            "id": "issue-to-pr-v1",
            "name": "stage one",
            "schema_version": 1,
            "nodes": {
                "node-1": {
                    "kind": node_kind,
                    "input_schema": {},
                    "output_schema": {},
                    "actor_type": actor_type,
                    "side_effect_class": "none",
                    "idempotency_behavior": "not_applicable",
                }
            },
            "terminal_states": ["complete"],
            "terminal_state_mapping": {"node-1": "complete"},
        }
    )


def test_tick_persists_human_wait_and_resumes_with_fresh_attempt(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    first = _request(
        repository=repository,
        revision=revision,
        workspace=tmp_path / "workspace",
        attempt_id="attempt-1",
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )
    limits = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)

    initial = runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=limits,
        requests=(first,),
    )

    assert len(initial.dispatched) == 1
    runtime.enter_human_wait(
        dispatched=initial.dispatched[0],
        reason="operator review required",
        now=now + timedelta(seconds=1),
    )
    waiting = ledger_service.read_projection(
        aggregate_type="runtime_node",
        aggregate_id="run-1:node-1",
    )
    assert waiting is not None
    assert waiting.state["status"] == "awaiting_human"
    assert waiting.state["workflow_definition_id"] == "issue-to-pr-v1"
    waiting_lease = ledger_service.read_lease(run_id="run-1", node_id="node-1")
    assert waiting_lease is not None
    assert waiting_lease.expires_at == (now + timedelta(seconds=1)).isoformat()

    resumed = _request(
        repository=repository,
        revision=revision,
        workspace=tmp_path / "workspace",
        attempt_id="attempt-2",
        operation_id="operation-2",
    )
    runtime.resume_human_wait(
        request=resumed,
        epoch=initial.epoch.epoch,
        now=now + timedelta(seconds=2),
    )
    next_tick = runtime.tick(
        now=now + timedelta(seconds=3),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=limits,
    )

    assert next_tick.dispatched[0].lease.fencing_token == 2
    runtime.enter_human_wait(
        dispatched=next_tick.dispatched[0],
        reason="cleanup",
        now=now + timedelta(seconds=4),
    )


def test_tick_accepts_manifest_bound_agent_dispatch(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    request = _request(
        repository=repository,
        revision=revision,
        workspace=tmp_path / "workspace",
        attempt_id="attempt-1",
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")

    tick = runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(WorkflowDispatch(request=request, manifest=_manifest()),),
    )

    assert len(tick.dispatched) == 1
    runtime.enter_human_wait(
        dispatched=tick.dispatched[0],
        reason="cleanup",
        now=now + timedelta(seconds=1),
    )


def test_workflow_dispatch_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(
        repository=repository,
        revision=revision,
        workspace=tmp_path / "workspace",
        attempt_id="attempt-1",
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )

    with pytest.raises(InvalidInputError, match="manifest identity"):
        WorkflowDispatch(
            request=replace(request, workflow_definition_id="another-workflow"),
            manifest=_manifest(),
        )


def test_workflow_dispatch_rejects_unknown_manifest_node(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(
        repository=repository,
        revision=revision,
        workspace=tmp_path / "workspace",
        attempt_id="attempt-1",
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )

    with pytest.raises(InvalidInputError, match="unknown manifest node"):
        WorkflowDispatch(request=replace(request, node_id="missing-node"), manifest=_manifest())


def test_workflow_dispatch_rejects_non_agent_manifest_node(
    tmp_path: Path,
) -> None:
    repository, revision = _repository(tmp_path)
    request = _request(
        repository=repository,
        revision=revision,
        workspace=tmp_path / "workspace",
        attempt_id="attempt-1",
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )

    with pytest.raises(InvalidInputError, match="agent-task"):
        WorkflowDispatch(request=request, manifest=_manifest(agent_node=False))
