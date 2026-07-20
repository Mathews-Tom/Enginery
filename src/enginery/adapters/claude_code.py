"""Claude Code CLI harness adapter with versioned stream-json event normalization."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterEventKind,
    AdapterFingerprint,
    AdapterStatus,
    NormalizedAdapterEvent,
    ProviderKind,
)
from enginery.application.work_ports import (
    HarnessOutput,
    HarnessResult,
    HarnessSession,
    HarnessTask,
)
from enginery.domain.artifact import RedactionClassification
from enginery.domain.errors import CancellationError, InvalidInputError, WorkerFailureError
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.redaction import redact_credential_shaped_text

_CLAUDE_CODE_EVENT_SCHEMA_VERSION = 1
_KNOWN_EVENT_TYPES = frozenset({"system", "assistant", "user", "rate_limit_event", "result"})


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
    artifact_store: ArtifactStore
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
    terminator: Callable[[int], None] = field(default=lambda pid: os.killpg(pid, signal.SIGTERM))
    _sessions: dict[str, HarnessSession] = field(default_factory=dict, init=False)
    _processes: dict[str, _ClaudeCodeProcess] = field(default_factory=dict, init=False)
    _collected: dict[str, tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]] = field(
        default_factory=dict, init=False
    )
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    _cancelled: set[str] = field(default_factory=set, init=False)

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
        self._outcomes[str(task.operation_id)] = ReconciliationResult.FOUND_MATCHING
        return session

    def events(self, session: HarnessSession) -> Iterator[NormalizedAdapterEvent]:
        return iter(self._collect(session)[0])

    def result(self, session: HarnessSession) -> HarnessResult:
        return self._collect(session)[1]

    def cancel(self, session: HarnessSession, *, operation_id: OperationId) -> ReconciliationResult:
        self._require_session(session)
        process = self._processes[session.session_id]
        if process.poll() is None:
            self.terminator(process.pid)
            self._cancelled.add(session.session_id)
        self._outcomes[str(operation_id)] = ReconciliationResult.FOUND_MATCHING
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return self._outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)

    def _collect(
        self, session: HarnessSession
    ) -> tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]:
        self._require_session(session)
        cached = self._collected.get(session.session_id)
        if cached is not None:
            return cached
        process = self._processes[session.session_id]
        output, _ = process.communicate()
        redacted = redact_credential_shaped_text(output)
        digest = self.artifact_store.publish_bytes(redacted.encode(), media_type="application/json")
        events, terminal_status = _normalize_events(redacted, session.operation_id)
        status = "cancelled" if session.session_id in self._cancelled else terminal_status
        result = HarnessResult(
            session_id=session.session_id,
            terminal_status=status,
            outputs=(HarnessOutput(digest=digest, redaction=RedactionClassification.SENSITIVE),),
        )
        collected = (events, result)
        self._collected[session.session_id] = collected
        return collected

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

    def _require_session(self, session: HarnessSession) -> None:
        if self._sessions.get(session.session_id) != session:
            raise CancellationError("unknown Claude Code harness session")

    def supervised_command(self, task: HarnessTask, *, result_path: Path) -> tuple[str, ...]:
        """Return a worker command whose parent is the coordinator supervisor.

        Reuses the existing generic supervised-worker entrypoint: it only
        runs the given command and redacts/records its exit, independent of
        which harness CLI is being wrapped.
        """
        if not result_path.is_absolute():
            raise InvalidInputError("Claude Code worker result path must be absolute")
        return (
            sys.executable,
            "-m",
            "enginery.engine.omp_worker",
            "--operation-id",
            str(task.operation_id),
            "--output",
            str(result_path),
            "--",
            *self.command_for(task),
        )

    def collect_supervised(
        self, task: HarnessTask, *, result_path: Path
    ) -> tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]:
        """Read a supervised worker handoff after its process exit is observed."""
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkerFailureError(
                "Claude Code supervised worker did not retain a valid result"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {
            "operation_id",
            "output",
            "schema_version",
            "terminal_status",
        }:
            raise WorkerFailureError("Claude Code supervised worker result has an invalid shape")
        operation_id = payload["operation_id"]
        output = payload["output"]
        schema_version = payload["schema_version"]
        terminal_status = payload["terminal_status"]
        if (
            not isinstance(operation_id, str)
            or not isinstance(output, str)
            or not isinstance(schema_version, int)
            or not isinstance(terminal_status, str)
        ):
            raise WorkerFailureError("Claude Code supervised worker result has invalid field types")
        if schema_version != 1:
            raise WorkerFailureError(
                "Claude Code supervised worker result schema version is unsupported"
            )
        if operation_id != str(task.operation_id):
            raise WorkerFailureError(
                "Claude Code supervised worker result operation does not match task"
            )
        digest = self.artifact_store.publish_bytes(
            output.encode("utf-8"), media_type="application/json"
        )
        events = (
            _normalize_events(output, task.operation_id)[0]
            if terminal_status == "succeeded"
            else ()
        )
        return (
            events,
            HarnessResult(
                session_id=f"claude-code-{task.operation_id}",
                terminal_status=terminal_status,
                outputs=(
                    HarnessOutput(digest=digest, redaction=RedactionClassification.SENSITIVE),
                ),
            ),
        )


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


def _normalize_events(
    output: str, operation_id: OperationId
) -> tuple[tuple[NormalizedAdapterEvent, ...], str]:
    events: list[NormalizedAdapterEvent] = []
    terminal_status: str | None = None
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            payload = cast(dict[str, object], json.loads(line))
        except json.JSONDecodeError as error:
            raise WorkerFailureError("Claude Code emitted malformed JSON event") from error
        event_type = payload.get("type")
        if not isinstance(event_type, str) or event_type not in _KNOWN_EVENT_TYPES:
            raise WorkerFailureError("Claude Code emitted an unsupported JSON event")
        attributes = {"event_schema_version": str(_CLAUDE_CODE_EVENT_SCHEMA_VERSION)}
        if event_type == "system" and payload.get("subtype") == "init":
            kind = AdapterEventKind.STARTED
        elif event_type == "result":
            kind = AdapterEventKind.TERMINAL
            terminal_status = "failed" if payload.get("is_error") else "succeeded"
        elif event_type == "rate_limit_event":
            kind = AdapterEventKind.DIAGNOSTIC
        else:
            kind = AdapterEventKind.PROGRESS
            model = _assistant_model(payload)
            if model is not None:
                attributes["model"] = model
        events.append(
            NormalizedAdapterEvent(
                kind=kind,
                occurred_at=datetime.now(UTC),
                operation_id=operation_id,
                summary=f"Claude Code {event_type}",
                attributes=attributes,
            )
        )
    if terminal_status is None:
        raise WorkerFailureError("Claude Code output ended without a result event")
    return tuple(events), terminal_status


def _assistant_model(payload: dict[str, object]) -> str | None:
    if payload.get("type") != "assistant":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    model = message.get("model")
    return model if isinstance(model, str) and model.strip() else None


__all__ = ["ClaudeCodeAdapterConfig", "ClaudeCodeHarness"]
