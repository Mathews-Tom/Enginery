"""Receipts and strict-effective holds recorded through the real supervised execution path."""

from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.execution import selection_for

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessResult, HarnessTask
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.implementation import Stage1ImplementationExecutor

_OMP_PIN = "openai/gpt-5.2"
_CLAUDE_PIN = "claude-opus-4-8"
_PIN = _OMP_PIN
_BANNERS = {"omp": "omp v18.10.3\n", "claude-code": "2.1.296 (Claude Code)\n"}
_MODEL_FLAG_PINS = {"omp": _OMP_PIN, "claude-code": _CLAUDE_PIN}
_EFFORT_FLAGS = {"omp": "--thinking", "claude-code": "--effort"}
_HOSTS = ("omp", "claude-code")


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


def _fake_host(path: Path, host: str, *, reported: tuple[str, ...]) -> Path:
    """An executable that prints a host-shaped event stream reporting ``reported`` models."""
    events: list[dict[str, object]] = []
    if host == "omp":
        events += [{"type": "session"}, {"type": "agent_start"}]
        for identifier in reported:
            provider, _, model = identifier.partition("/")
            events.append(
                {
                    "type": "message_end",
                    "message": {"role": "assistant", "provider": provider, "model": model},
                }
            )
        events.append({"type": "agent_end"})
    else:
        events.append({"type": "system", "subtype": "init"})
        for identifier in reported:
            events.append({"type": "assistant", "message": {"model": identifier}})
        events.append({"type": "result", "subtype": "success", "is_error": False})
    executable = path / f"fake-{host}"
    executable.write_text(
        f"#!/usr/bin/env python3\nimport json\nfor event in {events!r}:\n"
        "    print(json.dumps(event))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _harness(tmp_path: Path, host: str, artifacts: str) -> OmpHarness | ClaudeCodeHarness:
    executable = str(tmp_path / f"fake-{host}")

    def banner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, stdout=_BANNERS[host], stderr="")

    store = ArtifactStore(tmp_path / artifacts)
    if host == "omp":
        return OmpHarness(
            OmpAdapterConfig(credential_reference="profile", executable=executable),
            store,
            command_runner=banner,
        )
    return ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="profile", executable=executable),
        store,
        command_runner=banner,
    )


def _attempt(
    ledger_service: LedgerService,
    tmp_path: Path,
    *,
    selection: ExecutionSelection,
    reported: tuple[str, ...],
    launched_command: tuple[str, ...] | None | str = "persisted",
) -> tuple[HarnessResult, str, dict[str, object]]:
    host = selection.harness
    now = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)
    repository, revision = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _fake_host(tmp_path, host, reported=reported)
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
    executor = Stage1ImplementationExecutor(
        runtime, _harness(tmp_path, host, "artifacts"), _manifest()
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
    collector = Stage1ImplementationExecutor(
        runtime, _harness(tmp_path, host, "collected"), _manifest()
    )
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


def _strict(host: str, **kwargs: object) -> ExecutionSelection:
    return selection_for(host, model=_MODEL_FLAG_PINS[host], strict_effective=True, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", _HOSTS)
def test_the_receipt_records_submitted_flags_and_the_observed_model_for_a_pinned_run(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    pin = _MODEL_FLAG_PINS[host]
    selection = selection_for(host, model=pin, effort="high")

    result, status, details = _attempt(
        ledger_service, tmp_path, selection=selection, reported=(pin,)
    )

    assert status == "passed"
    assert result.execution_receipt is not None
    receipt = details["execution_receipt"]
    assert isinstance(receipt, dict)
    assert receipt["submitted_arguments"] == ["--model", pin, _EFFORT_FLAGS[host], "high"]
    assert receipt["effective_model"]["value"] == pin  # type: ignore[index]
    assert receipt["effective_effort"]["state"] == "unknown"  # type: ignore[index]
    assert receipt["effective_effort"]["value"] is None  # type: ignore[index]
    assert details["execution_selection_digest"] == str(selection.digest)
    assert "execution_hold" not in details


def test_the_effective_model_evidence_description_comes_from_the_catalog(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    result, _, _ = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for("omp", model=_OMP_PIN),
        reported=(_OMP_PIN,),
    )

    assert result.execution_receipt is not None
    assert result.execution_receipt.effective_model.source == (
        "message_end.message.provider and message_end.message.model"
    )
    assert "no effective-effort evidence" in result.execution_receipt.effective_effort.source


@pytest.mark.parametrize("host", _HOSTS)
def test_without_a_strict_requirement_a_different_served_model_is_recorded_not_hidden(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    served = "openai/gpt-5.3" if host == "omp" else "claude-sonnet-5-5"
    result, status, details = _attempt(
        ledger_service,
        tmp_path,
        selection=selection_for(host, model=_MODEL_FLAG_PINS[host]),
        reported=(served,),
    )

    assert status == "passed"
    assert result.execution_receipt is not None
    assert result.execution_receipt.effective_model.value == served
    assert result.execution_receipt.effective_model.value != _MODEL_FLAG_PINS[host]
    assert "execution_hold" not in details


@pytest.mark.parametrize("host", _HOSTS)
def test_a_strict_requirement_passes_only_when_the_effective_model_is_observed_equal(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    _, status, details = _attempt(
        ledger_service, tmp_path, selection=_strict(host), reported=(_MODEL_FLAG_PINS[host],)
    )

    assert status == "passed"
    assert "execution_hold" not in details


@pytest.mark.parametrize("host", _HOSTS)
def test_a_strict_requirement_holds_when_the_effective_model_is_unknown(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    _, status, details = _attempt(ledger_service, tmp_path, selection=_strict(host), reported=())

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "effective_control_unverified"


@pytest.mark.parametrize("host", _HOSTS)
def test_a_strict_requirement_holds_when_the_effective_model_differs(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    served = "openai/gpt-5.3" if host == "omp" else "claude-sonnet-5-5"
    _, status, details = _attempt(
        ledger_service, tmp_path, selection=_strict(host), reported=(served,)
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "effective_control_mismatch"


@pytest.mark.parametrize("host", _HOSTS)
def test_a_strict_pin_holds_when_the_host_reports_two_distinct_models(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    pin = _MODEL_FLAG_PINS[host]
    other = "openai/gpt-5.3" if host == "omp" else "claude-sonnet-5-5"

    result, status, details = _attempt(
        ledger_service, tmp_path, selection=_strict(host), reported=(pin, other)
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "effective_control_mismatch"
    assert result.execution_receipt is not None
    assert other in str(result.execution_receipt.effective_model.value)
    assert pin in str(result.execution_receipt.effective_model.value)


def test_a_crafted_strict_effort_pin_is_rejected_before_any_worker_is_launched(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    crafted = replace(selection_for("omp", model=_PIN, effort="high"), strict_effective=True)

    with pytest.raises(InvalidInputError, match="does not report its effective effort"):
        _attempt(ledger_service, tmp_path, selection=crafted, reported=(_PIN,))

    assert not (tmp_path / "workspace" / ".enginery").exists()


@pytest.mark.parametrize("host", _HOSTS)
@pytest.mark.parametrize(
    "tamper",
    [
        lambda command, host: ("--fallback-model", "claude-haiku-4-5", *command),
        lambda command, host: tuple(
            token for token in command if token not in ("--model", _MODEL_FLAG_PINS[host])
        ),
        lambda command, host: (*command, "--model", "openai/gpt-5.3"),
        lambda command, host: ("omp", "--mode=json"),
    ],
    ids=["extra-fallback", "missing-pin", "second-model", "controls-absent"],
)
def test_launched_controls_that_are_not_exactly_the_selected_ones_hold_the_run(
    ledger_service: LedgerService, tmp_path: Path, host: str, tamper: object
) -> None:
    pin = _MODEL_FLAG_PINS[host]
    selection = selection_for(host, model=pin)
    (tmp_path / "second").mkdir()
    runtime_command = _persisted_command(ledger_service, tmp_path, selection)
    assert callable(tamper)

    _, status, details = _attempt(
        ledger_service,
        tmp_path / "second",
        selection=selection,
        reported=(pin,),
        launched_command=tuple(tamper(runtime_command, host)),
    )

    assert status == "failed"
    hold = details["execution_hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "submitted_controls_mismatch"
    assert hold["details"]["expected_arguments"] == f"--model {pin}"  # type: ignore[index]


def _persisted_command(
    ledger_service: LedgerService, tmp_path: Path, selection: ExecutionSelection
) -> tuple[str, ...]:
    """The command the real adapter would persist for ``selection``."""
    host = selection.harness
    harness = _harness(tmp_path, host, "probe-artifacts")
    task = HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId("operation-1"),
        workspace_path=tmp_path / "workspace",
        objective="Make one bounded change.",
        acceptance_criteria=("tests pass",),
        constraints=("retain evidence",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=30,
        cost_budget=Decimal("1.00"),
        selection=selection,
    )
    return harness.command_for(task)


@pytest.mark.parametrize("host", _HOSTS)
def test_an_untampered_launched_command_for_a_host_default_run_has_no_control_tokens(
    ledger_service: LedgerService, tmp_path: Path, host: str
) -> None:
    result, status, details = _attempt(
        ledger_service, tmp_path, selection=selection_for(host), reported=()
    )

    assert status == "passed"
    assert result.execution_receipt is not None
    assert result.execution_receipt.submitted_arguments == ()
    assert "execution_hold" not in details
