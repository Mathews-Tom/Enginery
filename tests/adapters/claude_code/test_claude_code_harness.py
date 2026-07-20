from __future__ import annotations

import json
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.application.work_ports import HarnessTask
from enginery.domain.errors import CancellationError, WorkerFailureError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.artifact_store import ArtifactStore


class RecordedProcess:
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


def _task(workspace: Path, *, operation_id: str = "claude-code-start-1") -> HarnessTask:
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


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=(), returncode=returncode, stdout=stdout, stderr="")


def _events(*, include_terminal: bool = True, is_error: bool = False, secret: bool = False) -> str:
    records: list[dict[str, object]] = [
        {"type": "system", "subtype": "init", "session_id": "s1", "cwd": "/workspace"},
        {
            "type": "assistant",
            "message": {"model": "claude-haiku-4-5-20251001", "content": [{"type": "text"}]},
        },
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
    ]
    if secret:
        records.append(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-haiku-4-5-20251001",
                    "content": [{"type": "text", "text": "token: ghp_" + "a" * 40}],
                },
            }
        )
    records.append({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
    if include_terminal:
        records.append(
            {"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error}
        )
    return "\n".join(json.dumps(record) for record in records)


def _harness(
    tmp_path: Path,
    *,
    process: RecordedProcess | None = None,
    command_runner: object = None,
    terminator: object = None,
) -> ClaudeCodeHarness:
    kwargs: dict[str, object] = {}
    if process is not None:
        kwargs["process_factory"] = lambda arguments, workspace: process
    if command_runner is not None:
        kwargs["command_runner"] = command_runner
    if terminator is not None:
        kwargs["terminator"] = terminator
    return ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        ArtifactStore(tmp_path / "artifacts"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_probe_reports_available_with_fingerprint_when_cli_reports_a_version(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path, command_runner=lambda arguments: _completed("2.1.215 (Claude Code)\n")
    )

    status = harness.probe()

    assert status.availability.value == "available"
    assert status.fingerprint is not None
    assert status.fingerprint.provider_id == "claude-code-harness"
    assert "2.1.215 (Claude Code)" in status.fingerprint.provider_version
    capability_names = {capability.name for capability in status.fingerprint.capabilities}
    assert capability_names == {
        "stream_json_events",
        "cancellation",
        "redacted_artifacts",
        "structured_output",
    }


def test_probe_reports_unavailable_when_the_cli_is_missing(tmp_path: Path) -> None:
    def missing(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        raise OSError("executable not found")

    harness = _harness(tmp_path, command_runner=missing)

    status = harness.probe()

    assert status.availability.value == "unavailable"
    assert status.fingerprint is None


@pytest.mark.parametrize(
    "stdout,returncode",
    [
        ("", 0),
        ("not the expected banner\n", 0),
        ("2.1.215 (Claude Code)\n", 1),
    ],
)
def test_probe_reports_misconfigured_for_an_incompatible_response(
    tmp_path: Path, stdout: str, returncode: int
) -> None:
    harness = _harness(tmp_path, command_runner=lambda arguments: _completed(stdout, returncode))

    status = harness.probe()

    assert status.availability.value == "misconfigured"
    assert status.fingerprint is None


def test_start_launches_a_scoped_headless_command(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    workspaces: list[Path] = []

    def start(arguments: tuple[str, ...], workspace: Path) -> RecordedProcess:
        commands.append(arguments)
        workspaces.append(workspace)
        return RecordedProcess(_events())

    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=start,
    )

    session = harness.start(_task(tmp_path))

    assert session.session_id == "claude-code-claude-code-start-1"
    assert workspaces == [tmp_path]
    command = commands[0]
    assert command[0] == "claude"
    assert "-p" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--no-session-persistence" in command
    assert "--bare" not in command
    assert "--model" not in command
    assert "--api-key" not in command
    session_id_index = command.index("--session-id") + 1
    assert uuid.UUID(command[session_id_index])
    prompt = command[command.index("-p") + 1]
    assert "Objective" in prompt
    assert "Implement the requested provider behavior." in prompt
    assert "repository-write" in prompt
    assert harness.reconcile(operation_id=OperationId("claude-code-start-1")) == (
        ReconciliationResult.FOUND_MATCHING
    )


def test_start_passes_a_configured_model_override(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(
            credential_reference="operator-oauth-profile", model="claude-opus-4-8"
        ),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=lambda arguments, workspace: RecordedProcess(_events()),
    )

    command = harness.command_for(_task(tmp_path))

    assert "--model" in command
    assert command[command.index("--model") + 1] == "claude-opus-4-8"


def test_start_rejects_a_duplicate_session_for_the_same_operation(tmp_path: Path) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(_events()))
    harness.start(_task(tmp_path))

    with pytest.raises(WorkerFailureError):
        harness.start(_task(tmp_path))


def test_session_id_is_stable_for_the_same_operation_id(tmp_path: Path) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(_events()))

    first = harness.command_for(_task(tmp_path, operation_id="stable-op"))
    second = harness.command_for(_task(tmp_path, operation_id="stable-op"))

    first_uuid = first[first.index("--session-id") + 1]
    second_uuid = second[second.index("--session-id") + 1]
    assert first_uuid == second_uuid


def test_harness_normalizes_events_and_redacts_artifact_output(tmp_path: Path) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(_events(secret=True)))
    session = harness.start(_task(tmp_path))

    events = tuple(harness.events(session))
    result = harness.result(session)

    assert [event.kind.value for event in events] == [
        "started",
        "progress",
        "progress",
        "progress",
        "diagnostic",
        "terminal",
    ]
    assert result.terminal_status == "succeeded"
    assert result.outputs[0].redaction.value == "sensitive"
    assert ("ghp_" + "a" * 40).encode() not in harness.artifact_store.read_bytes(
        result.outputs[0].digest
    )


def test_harness_reports_the_observed_model_on_assistant_events(tmp_path: Path) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(_events()))
    session = harness.start(_task(tmp_path))

    events = tuple(harness.events(session))

    assistant_events = [event for event in events if "model" in event.attributes]
    assert assistant_events
    assert assistant_events[0].attributes["model"] == "claude-haiku-4-5-20251001"


def test_harness_reports_a_failed_terminal_status_for_an_error_result(tmp_path: Path) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(_events(is_error=True)))
    session = harness.start(_task(tmp_path))

    result = harness.result(session)

    assert result.terminal_status == "failed"


@pytest.mark.parametrize(
    "output,match",
    [
        ("not json", "malformed"),
        (json.dumps({"type": "unknown_event"}), "unsupported"),
        (json.dumps({"type": "assistant"}), "without a result event"),
    ],
)
def test_harness_rejects_malformed_unknown_or_incomplete_events(
    tmp_path: Path, output: str, match: str
) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(output))
    session = harness.start(_task(tmp_path))

    with pytest.raises(WorkerFailureError, match=match):
        tuple(harness.events(session))


def test_harness_cancels_the_process_group(tmp_path: Path) -> None:
    process = RecordedProcess(_events(), returncode=-15)
    terminated: list[int] = []
    harness = _harness(tmp_path, process=process, terminator=terminated.append)
    session = harness.start(_task(tmp_path))
    process.running = True

    outcome = harness.cancel(session, operation_id=OperationId("claude-code-start-1"))

    assert outcome is ReconciliationResult.FOUND_MATCHING
    assert terminated == [process.pid]
    result = harness.result(session)
    assert result.terminal_status == "cancelled"


def test_reconcile_reports_not_found_before_a_session_starts(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    outcome = harness.reconcile(operation_id=OperationId("never-started"))

    assert outcome is ReconciliationResult.NOT_FOUND


def test_events_and_cancel_reject_an_unknown_session(tmp_path: Path) -> None:
    harness = _harness(tmp_path, process=RecordedProcess(_events()))
    real_session = harness.start(_task(tmp_path))
    forged_session = type(real_session)(
        session_id="not-a-real-session", operation_id=real_session.operation_id
    )

    with pytest.raises(CancellationError):
        tuple(harness.events(forged_session))
    with pytest.raises(CancellationError):
        harness.cancel(forged_session, operation_id=real_session.operation_id)
