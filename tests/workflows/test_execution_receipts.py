"""Receipts and strict-effective holds recorded through the real supervised execution path."""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.execution import selection_for

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessResult, HarnessTask
from enginery.domain.errors import ExternalConflictError
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.implementation import Stage1ImplementationExecutor

_PIN = "openai/gpt-5.2"


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


def _fake_omp(path: Path, *, reported: tuple[str, str] | None) -> Path:
    events: list[dict[str, object]] = [{"type": "session"}, {"type": "agent_start"}]
    if reported is not None:
        provider, model = reported
        events.append(
            {
                "type": "message_end",
                "message": {"role": "assistant", "provider": provider, "model": model},
            }
        )
    events.append({"type": "agent_end"})
    executable = path / "omp"
    executable.write_text(
        f"#!/usr/bin/env python3\nimport json\nfor event in {events!r}:\n"
        "    print(json.dumps(event))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _harness(tmp_path: Path, artifacts: str) -> OmpHarness:
    return OmpHarness(
        OmpAdapterConfig(credential_reference="profile", executable=str(_fake_omp_path(tmp_path))),
        ArtifactStore(tmp_path / artifacts),
        command_runner=lambda arguments: subprocess.CompletedProcess(
            arguments, 0, stdout="omp v18.10.2\n", stderr=""
        ),
    )


def _fake_omp_path(tmp_path: Path) -> Path:
    return tmp_path / "omp"


def _attempt(
    ledger_service: LedgerService,
    tmp_path: Path,
    *,
    selection: ExecutionSelection,
    reported: tuple[str, str] | None,
    launched_command: tuple[str, ...] | None | str = "persisted",
) -> tuple[HarnessResult, str, dict[str, object]]:
    now = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _fake_omp(tmp_path, reported=reported)
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
        selection=selection,
    )
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    executor = Stage1ImplementationExecutor(runtime, _harness(tmp_path, "artifacts"), _manifest())
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
            retain_workspace=False,
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
    persisted = runtime.read_node_request(run_id="run-1", node_id="implement").command
    command = persisted if launched_command == "persisted" else launched_command
    assert command is None or isinstance(command, tuple)
    result_path = workspace / ".enginery" / "implementation-results" / "operation-1.json"
    collector = Stage1ImplementationExecutor(runtime, _harness(tmp_path, "collected"), _manifest())
    deadline = time.monotonic() + 10
    while True:
        if result_path.is_file():
            try:
                result = collector.collect(
                    dispatched=runtime.recover_dispatched(run_id="run-1", node_id="implement"),
                    task=task,
                    now=now + timedelta(seconds=1),
                    launched_command=command,
                )
            except ExternalConflictError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
            else:
                break
        elif time.monotonic() >= deadline:
            pytest.fail("supervised worker did not retain a result")
        else:
            time.sleep(0.01)
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:implement"
    )
    attempt = ledger_service.read_projection(
        aggregate_type="node_attempt", aggregate_id="attempt-1"
    )
    assert node is not None and attempt is not None
    details = attempt.state["result"]
    assert isinstance(details, dict)
    return result, str(node.state["status"]), details


def test_the_receipt_records_submitted_flags_and_the_observed_model_for_a_pinned_run(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    selection = selection_for("omp", model=_PIN, effort="high")

    result, status, details = _attempt(
        ledger_service, tmp_path, selection=selection, reported=("openai", "gpt-5.2")
    )

    assert status == "passed"
    assert result.execution_receipt is not None
    receipt = details["execution_receipt"]
    assert isinstance(receipt, dict)
    assert receipt["submitted_arguments"] == ["--model", _PIN, "--thinking", "high"]
    assert receipt["effective_model"]["value"] == _PIN  # type: ignore[index]
    assert receipt["effective_effort"]["state"] == "unknown"  # type: ignore[index]
    assert receipt["effective_effort"]["value"] is None  # type: ignore[index]
    assert details["execution_selection_digest"] == str(selection.digest)
    assert "execution_hold" not in details


def test_without_a_strict_requirement_a_different_served_model_is_recorded_not_hidden(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    result, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_PIN),
        reported=("openai", "gpt-5.3"),
    )

    assert status == "passed"
    assert result.execution_receipt is not None
    assert result.execution_receipt.effective_model.value == "openai/gpt-5.3"
    assert result.execution_receipt.effective_model.value != _PIN
    assert "execution_hold" not in details


def test_a_strict_requirement_passes_only_when_the_effective_model_is_observed_equal(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_PIN, strict_effective=True),
        reported=("openai", "gpt-5.2"),
    )

    assert status == "passed"
    assert "execution_hold" not in details


def test_a_strict_requirement_holds_when_the_effective_model_is_unknown(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_PIN, strict_effective=True),
        reported=None,
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "effective_control_unverified"


def test_a_strict_requirement_holds_when_the_effective_model_differs(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_PIN, strict_effective=True),
        reported=("openai", "gpt-5.3"),
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "effective_control_mismatch"


def test_a_strict_effort_pin_always_holds_because_the_effective_effort_is_unknown(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_PIN, effort="high", strict_effective=True),
        reported=("openai", "gpt-5.2"),
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "effective_control_unverified"
    assert hold["details"]["control"] == "effort"  # type: ignore[index]


def test_a_receipt_whose_submitted_controls_are_not_in_the_launched_command_holds(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_PIN),
        reported=("openai", "gpt-5.2"),
        launched_command=("omp", "--mode=json"),
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "submitted_controls_mismatch"
