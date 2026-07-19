"""OMP CLI harness adapter with versioned JSON event normalization."""

from __future__ import annotations

import json
import os
import signal
import subprocess
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

_OMP_PROTOCOL_VERSION = 3
_KNOWN_EVENT_TYPES = frozenset(
    {
        "session",
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    }
)


class _OmpProcess(Protocol):
    pid: int
    returncode: int | None

    def communicate(self) -> tuple[str, str | None]: ...

    def poll(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class OmpAdapterConfig:
    """Opaque OMP CLI configuration without credential material."""

    credential_reference: str
    executable: str = "omp"
    protocol_version: int = _OMP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.credential_reference.strip():
            raise InvalidInputError("OMP credential reference must be non-blank")
        if not self.executable.strip():
            raise InvalidInputError("OMP executable must be non-blank")
        if self.protocol_version != _OMP_PROTOCOL_VERSION:
            raise InvalidInputError("OMP protocol version is unsupported")


@dataclass(slots=True)
class OmpHarness:
    """Run OMP in JSON mode and retain only redacted output artifacts."""

    config: OmpAdapterConfig
    artifact_store: ArtifactStore
    process_factory: Callable[[tuple[str, ...], Path], _OmpProcess] = field(
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
    _processes: dict[str, _OmpProcess] = field(default_factory=dict, init=False)
    _collected: dict[str, tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]] = field(
        default_factory=dict, init=False
    )
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    _cancelled: set[str] = field(default_factory=set, init=False)

    def probe(self) -> AdapterStatus:
        try:
            completed = subprocess.run(
                (self.config.executable, "--help"), check=False, capture_output=True, text=True
            )
        except OSError:
            return AdapterStatus(
                kind=ProviderKind.HARNESS,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="OMP CLI is unavailable",
            )
        if completed.returncode != 0 or not completed.stdout.startswith("omp v"):
            return AdapterStatus(
                kind=ProviderKind.HARNESS,
                availability=AdapterAvailability.MISCONFIGURED,
                fingerprint=None,
                detail="OMP CLI did not report a compatible version",
            )
        version = completed.stdout.splitlines()[0]
        return AdapterStatus(
            kind=ProviderKind.HARNESS,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="omp-harness",
                provider_version=f"{version};json-protocol={self.config.protocol_version}",
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability("json_events", _OMP_PROTOCOL_VERSION),
                    AdapterCapability("cancellation", 1),
                    AdapterCapability("redacted_artifacts", 1),
                ),
            ),
            detail="OMP harness is available",
        )

    def start(self, task: HarnessTask) -> HarnessSession:
        session = HarnessSession(
            session_id=f"omp-{task.operation_id}", operation_id=task.operation_id
        )
        if session.session_id in self._sessions:
            raise WorkerFailureError("OMP harness session already exists")
        process = self.process_factory(self._command(task), task.workspace_path)
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
        events = _normalize_events(redacted, session.operation_id)
        if session.session_id in self._cancelled:
            status = "cancelled"
        elif process.returncode == 0:
            status = "succeeded"
        else:
            status = "failed"
        result = HarnessResult(
            session_id=session.session_id,
            terminal_status=status,
            outputs=(HarnessOutput(digest=digest, redaction=RedactionClassification.SENSITIVE),),
        )
        collected = (events, result)
        self._collected[session.session_id] = collected
        return collected

    def _command(self, task: HarnessTask) -> tuple[str, ...]:
        return (
            self.config.executable,
            "--mode=json",
            "--no-session",
            "--cwd",
            str(task.workspace_path),
            "--max-time",
            str(task.time_budget_seconds),
            "-p",
            _render_task(task),
        )

    def _require_session(self, session: HarnessSession) -> None:
        if self._sessions.get(session.session_id) != session:
            raise CancellationError("unknown OMP harness session")


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


def _normalize_events(output: str, operation_id: OperationId) -> tuple[NormalizedAdapterEvent, ...]:
    events: list[NormalizedAdapterEvent] = []
    terminal_seen = False
    for line in output.splitlines():
        try:
            payload = cast(dict[str, object], json.loads(line))
        except json.JSONDecodeError as error:
            raise WorkerFailureError("OMP emitted malformed JSON event") from error
        event_type = payload.get("type")
        if not isinstance(event_type, str) or event_type not in _KNOWN_EVENT_TYPES:
            raise WorkerFailureError("OMP emitted an unsupported JSON event")
        kind = AdapterEventKind.PROGRESS
        if event_type == "agent_start":
            kind = AdapterEventKind.STARTED
        elif event_type == "agent_end":
            kind = AdapterEventKind.TERMINAL
            terminal_seen = True
        events.append(
            NormalizedAdapterEvent(
                kind=kind,
                occurred_at=datetime.now(UTC),
                operation_id=operation_id,
                summary=f"OMP {event_type}",
                attributes={"protocol_version": str(_OMP_PROTOCOL_VERSION)},
            )
        )
    if not terminal_seen:
        raise WorkerFailureError("OMP output ended without an agent_end event")
    return tuple(events)


__all__ = ["OmpAdapterConfig", "OmpHarness"]
