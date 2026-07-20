from __future__ import annotations

import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.application.work_ports import HarnessTask
from enginery.domain.errors import WorkerFailureError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId


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


def test_probe_reports_available_with_fingerprint_when_cli_reports_a_version() -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        command_runner=lambda arguments: _completed("2.1.215 (Claude Code)\n"),
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


def test_probe_reports_unavailable_when_the_cli_is_missing() -> None:
    def missing(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        raise OSError("executable not found")

    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        command_runner=missing,
    )

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
    stdout: str, returncode: int
) -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        command_runner=lambda arguments: _completed(stdout, returncode),
    )

    status = harness.probe()

    assert status.availability.value == "misconfigured"
    assert status.fingerprint is None


def test_start_launches_a_scoped_headless_command(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    workspaces: list[Path] = []

    def start(arguments: tuple[str, ...], workspace: Path) -> RecordedProcess:
        commands.append(arguments)
        workspaces.append(workspace)
        return RecordedProcess("")

    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        process_factory=start,
    )

    session = harness.start(_task(tmp_path))

    assert session.session_id == "claude-code-claude-code-start-1"
    assert workspaces == [tmp_path]
    command = commands[0]
    assert command[0] == "claude"
    assert "-p" in command
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--strict-mcp-config" in command
    assert "--permission-mode" in command
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


def test_start_passes_a_configured_model_override(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(
            credential_reference="operator-oauth-profile", model="claude-opus-4-8"
        ),
        process_factory=lambda arguments, workspace: RecordedProcess(""),
    )

    command = harness.command_for(_task(tmp_path))

    assert "--model" in command
    assert command[command.index("--model") + 1] == "claude-opus-4-8"


def test_start_rejects_a_duplicate_session_for_the_same_operation(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        process_factory=lambda arguments, workspace: RecordedProcess(""),
    )
    harness.start(_task(tmp_path))

    with pytest.raises(WorkerFailureError):
        harness.start(_task(tmp_path))


def test_session_id_is_stable_for_the_same_operation_id(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="operator-oauth-profile"),
        process_factory=lambda arguments, workspace: RecordedProcess(""),
    )

    first = harness.command_for(_task(tmp_path, operation_id="stable-op"))
    second = harness.command_for(_task(tmp_path, operation_id="stable-op"))

    first_uuid = first[first.index("--session-id") + 1]
    second_uuid = second[second.index("--session-id") + 1]
    assert first_uuid == second_uuid
