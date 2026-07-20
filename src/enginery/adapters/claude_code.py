"""Claude Code CLI harness adapter with versioned stream-json event normalization."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.work_ports import HarnessSession, HarnessTask
from enginery.domain.errors import InvalidInputError, WorkerFailureError

_CLAUDE_CODE_EVENT_SCHEMA_VERSION = 1


class _ClaudeCodeProcess(Protocol):
    pid: int
    returncode: int | None

    def communicate(self) -> tuple[str, str | None]: ...

    def poll(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class ClaudeCodeAdapterConfig:
    """Opaque Claude Code CLI configuration without credential material.

    ``model`` is an optional override; when blank, no ``--model`` flag is
    passed and Claude Code resolves its own configured default. The
    actually-selected model is read back from harness output, never assumed.
    """

    credential_reference: str
    executable: str = "claude"
    model: str = ""
    event_schema_version: int = _CLAUDE_CODE_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.credential_reference.strip():
            raise InvalidInputError("Claude Code credential reference must be non-blank")
        if not self.executable.strip():
            raise InvalidInputError("Claude Code executable must be non-blank")
        if self.event_schema_version != _CLAUDE_CODE_EVENT_SCHEMA_VERSION:
            raise InvalidInputError("Claude Code event schema version is unsupported")


@dataclass(slots=True)
class ClaudeCodeHarness:
    """Run Claude Code headless and retain only redacted output artifacts.

    Every launched command is scoped with ``--strict-mcp-config`` so a Stage 1
    worker task never inherits the operator's ambient MCP servers. ``--bare``
    is deliberately not used: it forces API-key-only authentication and
    rejects the OAuth/keychain credentials most operators already have
    configured via ``claude login``.
    """

    config: ClaudeCodeAdapterConfig
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = field(
        default=lambda arguments: subprocess.run(
            arguments, check=False, capture_output=True, text=True
        )
    )
    process_factory: Callable[[tuple[str, ...], Path], _ClaudeCodeProcess] = field(
        default=lambda arguments, workspace: subprocess.Popen(
            arguments,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    )
    _sessions: dict[str, HarnessSession] = field(default_factory=dict, init=False)
    _processes: dict[str, _ClaudeCodeProcess] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        try:
            completed = self.command_runner((self.config.executable, "--version"))
        except OSError:
            return AdapterStatus(
                kind=ProviderKind.HARNESS,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="Claude Code CLI is unavailable",
            )
        version = completed.stdout.strip()
        if completed.returncode != 0 or "Claude Code" not in version:
            return AdapterStatus(
                kind=ProviderKind.HARNESS,
                availability=AdapterAvailability.MISCONFIGURED,
                fingerprint=None,
                detail="Claude Code CLI did not report a compatible version",
            )
        return AdapterStatus(
            kind=ProviderKind.HARNESS,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="claude-code-harness",
                provider_version=(
                    f"{version};stream-json-schema={self.config.event_schema_version}"
                ),
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability("stream_json_events", self.config.event_schema_version),
                    AdapterCapability("cancellation", 1),
                    AdapterCapability("redacted_artifacts", 1),
                    AdapterCapability("structured_output", 1),
                ),
            ),
            detail="Claude Code harness is available",
        )

    def start(self, task: HarnessTask) -> HarnessSession:
        session = HarnessSession(
            session_id=f"claude-code-{task.operation_id}", operation_id=task.operation_id
        )
        if session.session_id in self._sessions:
            raise WorkerFailureError("Claude Code harness session already exists")
        process = self.process_factory(self.command_for(task), task.workspace_path)
        self._sessions[session.session_id] = session
        self._processes[session.session_id] = process
        return session

    def command_for(self, task: HarnessTask) -> tuple[str, ...]:
        arguments: list[str] = [
            self.config.executable,
            "-p",
            _render_task(task),
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "text",
            "--no-session-persistence",
            "--session-id",
            str(uuid.uuid5(uuid.NAMESPACE_URL, str(task.operation_id))),
            "--strict-mcp-config",
            "--permission-mode",
            "bypassPermissions",
        ]
        if self.config.model:
            arguments += ["--model", self.config.model]
        return tuple(arguments)


def _render_task(task: HarnessTask) -> str:
    sections = (
        ("Objective", (task.objective,)),
        ("Acceptance criteria", task.acceptance_criteria),
        ("Constraints", task.constraints),
        ("Permitted capabilities", task.permitted_capabilities),
        ("Evidence requirements", task.evidence_requirements),
    )
    return "\n\n".join(
        f"{heading}:\n" + "\n".join(f"- {value}" for value in values)
        for heading, values in sections
    )


__all__ = ["ClaudeCodeAdapterConfig", "ClaudeCodeHarness"]
