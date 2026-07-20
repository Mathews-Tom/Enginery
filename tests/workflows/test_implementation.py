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


@pytest.mark.parametrize(
    (
        "retain_workspace",
        "workspace_exists",
        "head_branch",
        "node_status",
        "recover_after_expiry",
    ),
    [
        (False, False, None, "passed", False),
        (True, True, None, "passed", False),
        (True, True, "enginery/run-1", "failed", False),
        (False, False, None, "passed", True),
    ],
)
def test_stage1_implementation_records_the_declared_workspace_lifecycle(
    ledger_service: LedgerService,
    tmp_path: Path,
    retain_workspace: bool,
    workspace_exists: bool,
    head_branch: str | None,
    node_status: str,
    recover_after_expiry: bool,
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
    runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=120) if recover_after_expiry else timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(dispatch,),
    )
    result_path = workspace / ".enginery" / "implementation-results" / "operation-1.json"
    collection_now = now + timedelta(seconds=1)
    recovered_runtime = CoordinatorRuntime(
        ledger_service,
        owner="replacement" if recover_after_expiry else "coordinator",
    )
    if recover_after_expiry:
        collection_now = now + timedelta(seconds=62)
        recovered_runtime.claim_epoch(
            now=collection_now - timedelta(seconds=1),
            heartbeat_window=timedelta(seconds=60),
        )
    recovered_executor = Stage1ImplementationExecutor(
        recovered_runtime,
        OmpHarness(
            OmpAdapterConfig(
                credential_reference="omp-auth-profile:default",
                executable=str(_omp_executable(tmp_path)),
            ),
            ArtifactStore(tmp_path / "recovered-artifacts"),
        ),
        _manifest(),
        head_branch=head_branch,
    )
    deadline = time.monotonic() + 5
    while True:
        if result_path.is_file():
            try:
                result = recovered_executor.collect(
                    dispatched=recovered_runtime.recover_dispatched(
                        run_id="run-1",
                        node_id="implement",
                    ),
                    task=task,
                    now=collection_now,
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
    assert node.state["status"] == node_status
    if head_branch is not None:
        attempt = ledger_service.read_projection(
            aggregate_type="node_attempt", aggregate_id="attempt-1"
        )
        assert attempt is not None
        result_details = attempt.state["result"]
        assert isinstance(result_details, dict)
        assert isinstance(result_details.get("branch_verification_error"), str)
    assert workspace.exists() is workspace_exists
    lifecycle_now = collection_now + timedelta(seconds=1)
    lifecycle_epoch = recovered_runtime.claim_epoch(
        now=lifecycle_now,
        heartbeat_window=timedelta(seconds=60),
    )
    if retain_workspace:
        cleaned = recovered_runtime.release_workspace(
            run_id="run-1",
            repository_id="repository-1",
            epoch=lifecycle_epoch.epoch,
            now=lifecycle_now,
        )
        assert cleaned.status == "cleaned"
        assert not workspace.exists()
    else:
        with pytest.raises(ExternalConflictError, match="workspace release requires a retained"):
            recovered_runtime.release_workspace(
                run_id="run-1",
                repository_id="repository-1",
                epoch=lifecycle_epoch.epoch,
                now=lifecycle_now,
            )


def test_stage1_implementation_retries_a_terminal_agent_node_with_a_fresh_attempt(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    harness = OmpHarness(
        OmpAdapterConfig(
            credential_reference="omp-auth-profile:default",
            executable=str(_omp_executable(tmp_path)),
        ),
        ArtifactStore(tmp_path / "artifacts"),
    )
    executor = Stage1ImplementationExecutor(runtime, harness, _manifest())

    def _run_attempt(attempt_id: str, operation_id: str) -> None:
        task = HarnessTask(
            run_id=RunId("run-1"),
            node_id=NodeId("implement"),
            attempt_id=NodeAttemptId(attempt_id),
            operation_id=OperationId(operation_id),
            workspace_path=workspace,
            objective="Make one bounded change.",
            acceptance_criteria=("tests pass",),
            constraints=("retain evidence",),
            permitted_capabilities=("repository-write",),
            evidence_requirements=("test report",),
            time_budget_seconds=30,
            cost_budget=Decimal("1.00"),
        )
        dispatch = executor.dispatch(
            FixtureDispatch(
                run_id="run-1",
                node_id="implement",
                attempt_id=attempt_id,
                repository_id="repository-1",
                repository_path=repository,
                workspace_path=workspace,
                base_revision=revision,
                command=("unreachable",),
                expected_attempt_version=0,
                operation_id=operation_id,
                workflow_definition_id="issue-to-pr-v1",
                retain_workspace=True,
            ),
            task,
        )
        runtime.tick(
            now=now,
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            requests=(dispatch,),
        )
        result_path = workspace / ".enginery" / "implementation-results" / f"{operation_id}.json"
        deadline = time.monotonic() + 5
        while True:
            if result_path.is_file():
                try:
                    executor.collect(
                        dispatched=runtime.recover_dispatched(run_id="run-1", node_id="implement"),
                        task=task,
                        now=now,
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

    _run_attempt("attempt-1", "operation-1")
    first_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:implement"
    )
    assert first_node is not None
    assert first_node.state["status"] == "passed"
    assert first_node.state["attempt_id"] == "attempt-1"
    reservation = ledger_service.read_process_manager_state(
        process_manager_name="workspace-reservations", state_key="repository-1"
    )
    assert reservation is not None
    assert reservation.state["status"] == "retained"

    _run_attempt("attempt-2", "operation-2")
    second_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:implement"
    )
    assert second_node is not None
    assert second_node.state["status"] == "passed"
    assert second_node.state["attempt_id"] == "attempt-2"
    assert second_node.state["operation_id"] == "operation-2"


def test_stage1_implementation_rejects_a_second_dispatch_while_the_first_is_still_active(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    harness = OmpHarness(
        OmpAdapterConfig(
            credential_reference="omp-auth-profile:default",
            executable=str(_omp_executable(tmp_path)),
        ),
        ArtifactStore(tmp_path / "artifacts"),
    )
    executor = Stage1ImplementationExecutor(runtime, harness, _manifest())
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
            retain_workspace=True,
        ),
        task,
    )
    runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(dispatch,),
    )
    retry_task = HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-2"),
        operation_id=OperationId("operation-2"),
        workspace_path=workspace,
        objective="Make one bounded change.",
        acceptance_criteria=("tests pass",),
        constraints=("retain evidence",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=30,
        cost_budget=Decimal("1.00"),
    )
    retry_dispatch = executor.dispatch(
        FixtureDispatch(
            run_id="run-1",
            node_id="implement",
            attempt_id="attempt-2",
            repository_id="repository-1",
            repository_path=repository,
            workspace_path=workspace,
            base_revision=revision,
            command=("unreachable",),
            expected_attempt_version=0,
            operation_id="operation-2",
            workflow_definition_id="issue-to-pr-v1",
            retain_workspace=True,
        ),
        retry_task,
    )
    with pytest.raises(ExternalConflictError, match="different immutable request"):
        runtime.tick(
            now=now,
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            requests=(retry_dispatch,),
        )
