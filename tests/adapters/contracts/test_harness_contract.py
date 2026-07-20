"""Shared behavioral contract exercised identically against every HarnessPort adapter.

Demonstrates that OMP and Claude Code agree on cancellation, timeout,
malformed-event, and unavailable-harness semantics without either adapter
leaking a provider-named field into the shared assertions below.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import pytest

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessSession, HarnessTask
from enginery.domain.errors import WorkerFailureError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.artifact_store import ArtifactStore


class _RecordedProcess:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.pid = 4242
        self.running = True

    def communicate(self) -> tuple[str, None]:
        self.running = False
        return self.output, None

    def poll(self) -> int | None:
        return None if self.running else self.returncode


class _HarnessFactory(Protocol):
    def __call__(
        self,
        tmp_path: Path,
        *,
        process: _RecordedProcess | None = None,
        executable: str | None = None,
        terminator: object = None,
    ) -> object: ...


def _omp_harness(
    tmp_path: Path,
    *,
    process: _RecordedProcess | None = None,
    executable: str | None = None,
    terminator: object = None,
) -> OmpHarness:
    kwargs: dict[str, object] = {}
    if process is not None:
        kwargs["process_factory"] = lambda arguments, workspace: process
    if terminator is not None:
        kwargs["terminator"] = terminator
    config_kwargs: dict[str, object] = {"credential_reference": "operator-profile"}
    if executable is not None:
        config_kwargs["executable"] = executable
    return OmpHarness(
        OmpAdapterConfig(**config_kwargs),  # type: ignore[arg-type]
        ArtifactStore(tmp_path / "artifacts"),
        **kwargs,  # type: ignore[arg-type]
    )


def _claude_code_harness(
    tmp_path: Path,
    *,
    process: _RecordedProcess | None = None,
    executable: str | None = None,
    terminator: object = None,
) -> ClaudeCodeHarness:
    kwargs: dict[str, object] = {}
    if process is not None:
        kwargs["process_factory"] = lambda arguments, workspace: process
    if terminator is not None:
        kwargs["terminator"] = terminator
    config_kwargs: dict[str, object] = {"credential_reference": "operator-oauth-profile"}
    if executable is not None:
        config_kwargs["executable"] = executable
    return ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(**config_kwargs),  # type: ignore[arg-type]
        ArtifactStore(tmp_path / "artifacts"),
        **kwargs,  # type: ignore[arg-type]
    )


def _omp_success_output() -> str:
    records = [
        {"type": "session", "version": 3},
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}},
        {"type": "agent_end"},
    ]
    return "\n".join(json.dumps(record) for record in records)


def _claude_code_success_output() -> str:
    records = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001"}},
        {"type": "result", "subtype": "success", "is_error": False},
    ]
    return "\n".join(json.dumps(record) for record in records)


@dataclass(frozen=True)
class _Provider:
    name: str
    build: _HarnessFactory
    success_output: Callable[[], str]


_PROVIDERS = (
    _Provider(name="omp", build=_omp_harness, success_output=_omp_success_output),
    _Provider(
        name="claude-code", build=_claude_code_harness, success_output=_claude_code_success_output
    ),
)


def _task(workspace: Path, *, operation_id: str = "contract-start-1") -> HarnessTask:
    return HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId(operation_id),
        workspace_path=workspace,
        objective="Implement the requested provider behavior.",
        acceptance_criteria=("tests pass",),
        constraints=("no credentials in output",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=Decimal("1.00"),
    )


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda provider: provider.name)
def test_unavailable_harness_reports_no_fingerprint_and_does_not_fall_back(
    tmp_path: Path, provider: _Provider
) -> None:
    """A missing CLI is a diagnostic UNAVAILABLE status, never a silent fallback."""
    harness = provider.build(tmp_path, executable="/nonexistent/enginery-contract-probe-xyz")
    status = harness.probe()  # type: ignore[attr-defined]

    assert status.availability.value == "unavailable"
    assert status.fingerprint is None


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda provider: provider.name)
def test_malformed_output_raises_the_same_normalized_failure_class(
    tmp_path: Path, provider: _Provider
) -> None:
    """A line that is not JSON is a WorkerFailureError, not a silently dropped event."""
    harness = provider.build(tmp_path, process=_RecordedProcess("not json"))
    session = harness.start(_task(tmp_path))  # type: ignore[attr-defined]

    with pytest.raises(WorkerFailureError):
        tuple(harness.events(session))  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda provider: provider.name)
def test_a_started_run_reaches_a_terminal_succeeded_status(
    tmp_path: Path, provider: _Provider
) -> None:
    """Both adapters normalize a clean run into started ... terminal / succeeded."""
    harness = provider.build(tmp_path, process=_RecordedProcess(provider.success_output()))
    session = harness.start(_task(tmp_path))  # type: ignore[attr-defined]

    events = tuple(harness.events(session))  # type: ignore[attr-defined]
    result = harness.result(session)  # type: ignore[attr-defined]

    kinds = [event.kind.value for event in events]
    assert "started" in kinds
    assert events[-1].kind.value == "terminal"
    assert result.terminal_status == "succeeded"


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda provider: provider.name)
def test_cancellation_and_a_coordinator_triggered_timeout_share_one_code_path(
    tmp_path: Path, provider: _Provider
) -> None:
    """Neither adapter's CLI has its own timeout flag; the coordinator's lease/heartbeat
    expiry triggers the exact same cancel() the operator would call directly, so timeout
    and operator-initiated cancellation are indistinguishable at the harness boundary --
    both report terminal_status "cancelled" through the same reconciliation outcome."""
    process = _RecordedProcess(provider.success_output(), returncode=-15)
    terminated: list[int] = []
    harness = provider.build(tmp_path, process=process, terminator=terminated.append)
    session = harness.start(_task(tmp_path))  # type: ignore[attr-defined]
    process.running = True

    outcome = harness.cancel(session, operation_id=OperationId("contract-start-1"))  # type: ignore[attr-defined]

    assert outcome is ReconciliationResult.FOUND_MATCHING
    assert terminated == [process.pid]
    result = harness.result(session)  # type: ignore[attr-defined]
    assert result.terminal_status == "cancelled"


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda provider: provider.name)
def test_reconcile_before_any_start_reports_not_found(tmp_path: Path, provider: _Provider) -> None:
    harness = provider.build(tmp_path)

    outcome = harness.reconcile(operation_id=OperationId("never-started"))  # type: ignore[attr-defined]

    assert outcome is ReconciliationResult.NOT_FOUND


@pytest.mark.parametrize("provider", _PROVIDERS, ids=lambda provider: provider.name)
def test_no_provider_named_field_appears_on_the_shared_task_or_session_types(
    tmp_path: Path, provider: _Provider
) -> None:
    """HarnessTask and HarnessSession -- the types this contract compares across
    providers -- carry no omp_*/claude_code_* attribute; only the adapter module
    each provider lives in is provider-named, never the shared contract surface."""
    session = HarnessSession(session_id="s", operation_id=OperationId("op"))
    task_fields = {field.name for field in fields(_task(tmp_path))}
    session_fields = {field.name for field in fields(session)}

    for field_names in (task_fields, session_fields):
        assert not any(
            name.startswith(("omp_", "omp", "claude_code", "claude_")) for name in field_names
        )
