from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessTask
from enginery.domain.errors import ExternalConflictError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.implementation import Stage1ImplementationExecutor


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


def _manifest() -> WorkflowManifest:
    return WorkflowManifest.from_mapping(
        {
            "id": "issue-to-pr-v1",
            "name": "stage one",
            "schema_version": 1,
            "nodes": {
                "implement": {
                    "kind": "execute_agent_task",
                    "input_schema": {},
                    "output_schema": {},
                    "actor_type": "agent",
                    "side_effect_class": "none",
                    "idempotency_behavior": "not_applicable",
                }
            },
            "terminal_states": ["complete"],
            "terminal_state_mapping": {"implement": "complete"},
        }
    )


def _omp_executable(path: Path) -> Path:
    executable = path / "omp"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "for event in ('session', 'agent_start', 'agent_end'):\n"
        "    print(json.dumps({'type': event}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize(("retain_workspace", "workspace_exists"), [(False, False), (True, True)])
def test_stage1_implementation_records_the_declared_workspace_lifecycle(
    ledger_service: LedgerService, tmp_path: Path, retain_workspace: bool, workspace_exists: bool
) -> None:
    now = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    task = HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId("operation-1"),
        workspace_path=workspace,
        objective="Make one bounded change.",
        acceptance_criteria=("tests pass",),
        constraints=("retain evidence",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=30,
        cost_budget=Decimal("1.00"),
    )
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    harness = OmpHarness(
        OmpAdapterConfig(
            credential_reference="omp-auth-profile:default",
            executable=str(_omp_executable(tmp_path)),
        ),
        ArtifactStore(tmp_path / "artifacts"),
    )
    executor = Stage1ImplementationExecutor(runtime, harness, _manifest())
    dispatch = executor.dispatch(
        FixtureDispatch(
            run_id="run-1",
            node_id="implement",
            attempt_id="attempt-1",
            repository_id="repository-1",
            repository_path=repository,
            workspace_path=workspace,
            base_revision=revision,
            command=("unreachable",),
            expected_attempt_version=0,
            operation_id="operation-1",
            workflow_definition_id="issue-to-pr-v1",
            retain_workspace=retain_workspace,
        ),
        task,
    )
    tick = runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(dispatch,),
    )
    result_path = workspace / ".enginery" / "omp-results" / "operation-1.json"
    deadline = time.monotonic() + 5
    while True:
        if result_path.is_file():
            try:
                result = executor.collect(
                    dispatched=tick.dispatched[0],
                    task=task,
                    now=now + timedelta(seconds=1),
                )
            except ExternalConflictError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
            else:
                break
        elif time.monotonic() >= deadline:
            pytest.fail("supervised OMP worker did not retain a result")
        else:
            time.sleep(0.01)

    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:implement"
    )
    assert result.terminal_status == "succeeded"
    assert node is not None
    assert node.state["status"] == "passed"
    assert workspace.exists() is workspace_exists
    if retain_workspace:
        epoch = runtime.claim_epoch(
            now=now + timedelta(seconds=2), heartbeat_window=timedelta(seconds=60)
        )
        cleaned = runtime.release_workspace(
            run_id="run-1",
            repository_id="repository-1",
            epoch=epoch.epoch,
            now=now + timedelta(seconds=2),
        )
        assert cleaned.status == "cleaned"
        assert not workspace.exists()
    else:
        epoch = runtime.claim_epoch(
            now=now + timedelta(seconds=2), heartbeat_window=timedelta(seconds=60)
        )
        with pytest.raises(ExternalConflictError, match="workspace release requires a retained"):
            runtime.release_workspace(
                run_id="run-1",
                repository_id="repository-1",
                epoch=epoch.epoch,
                now=now + timedelta(seconds=2),
            )
