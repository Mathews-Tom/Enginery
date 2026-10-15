"""OMP CLI harness adapter with versioned JSON event normalization."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from enginery.adapters.runner_launch import (
    isolated_command,
    publish_runner_receipt,
    read_attempt_result,
    relay_omp_seed_files,
    require_isolated_permissions,
    verified_image_host,
    worker_environment,
)
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
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.execution_controls import (
    OBSERVED_HOST_FLAG,
    build_receipt,
    launch_observation,
    prepare_native_controls,
)
from enginery.application.work_ports import (
    HarnessOutput,
    HarnessResult,
    HarnessSession,
    HarnessTask,
)
from enginery.domain.artifact import RedactionClassification
from enginery.domain.errors import CancellationError, InvalidInputError, WorkerFailureError
from enginery.domain.execution_catalog import ExecutionCatalog, InstalledHost
from enginery.domain.execution_receipt import ExecutionReceipt, RunnerReceiptRef
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.runner import (
    WORKER_HOME,
    WORKSPACE_MOUNT,
    PermissionMapping,
    RunnerContext,
    isolated_permission_mapping,
    trusted_local_permission_mapping,
)
from enginery.engine.omp_worker import OmpWorkerResult
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.redaction import redact_credential_shaped_text

_OMP_PROTOCOL_VERSION = 3
_HARNESS_ID = "omp"
_KNOWN_EVENT_TYPES = frozenset(
    {
        "session",
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
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
    catalog: ExecutionCatalog = field(default_factory=load_execution_catalog)
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = field(
        default=lambda arguments: subprocess.run(
            arguments, check=False, capture_output=True, text=True
        )
    )
    permissions: PermissionMapping | None = None
    """Overrides the mapping resolved from the trust mode (never a bypass when isolated)."""
    runner: RunnerContext | None = None
    """Set to run the worker in the isolated container profile instead of as the operator."""
    _image_host: InstalledHost | None = field(default=None, init=False)
    _sessions: dict[str, HarnessSession] = field(default_factory=dict, init=False)
    _tasks: dict[str, HarnessTask] = field(default_factory=dict, init=False)
    _commands: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _launch_versions: dict[str, str | None] = field(default_factory=dict, init=False)
    _processes: dict[str, _OmpProcess] = field(default_factory=dict, init=False)
    _collected: dict[str, tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]] = field(
        default_factory=dict, init=False
    )
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    _cancelled: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.runner is not None and self.permissions is not None:
            require_isolated_permissions(self.permissions)

    def resolved_permissions(self) -> PermissionMapping:
        """The permission mapping this harness launches with: declared, never implicit."""
        if self.permissions is not None:
            return self.permissions
        if self.runner is not None:
            return isolated_permission_mapping(_HARNESS_ID)
        return trusted_local_permission_mapping(_HARNESS_ID)

    def probe(self) -> AdapterStatus:
        try:
            completed = self.command_runner((self.config.executable, "--help"))
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

    def installed_host(self) -> InstalledHost | None:
        """Return the probed executable version and the adapter's declared protocol.

        An isolated harness is probed inside the pinned image, never on the host. The image is
        immutable for the run, so one probe is kept for the harness's lifetime.
        """
        if self.runner is None:
            return probe_installed_host(self.config.executable, self.command_runner)
        if self._image_host is None:
            self._image_host = probe_installed_host(self.config.executable, self.command_runner)
        return self._image_host

    def start(self, task: HarnessTask) -> HarnessSession:
        if self.runner is not None:
            raise WorkerFailureError(
                "an isolated OMP worker is started through the supervised launcher"
            )
        session = HarnessSession(
            session_id=f"omp-{task.operation_id}", operation_id=task.operation_id
        )
        if session.session_id in self._sessions:
            raise WorkerFailureError("OMP harness session already exists")
        command, observed = self._host_command(task)
        process = self.process_factory(command, task.workspace_path)
        self._sessions[session.session_id] = session
        self._tasks[session.session_id] = task
        self._commands[session.session_id] = command
        self._launch_versions[session.session_id] = observed
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
            execution_receipt=self._receipt(
                self._tasks[session.session_id],
                events,
                self._commands[session.session_id],
                observed_at_launch=self._launch_versions.get(session.session_id),
            ),
        )
        collected = (events, result)
        self._collected[session.session_id] = collected
        return collected

    def command_for(self, task: HarnessTask) -> tuple[str, ...]:
        return self._host_command(task)[0]

    def _host_command(self, task: HarnessTask) -> tuple[tuple[str, ...], str | None]:
        """The host command and the version observed while preparing its controls.

        Host paths for trusted-local, in-container paths when isolated.
        """
        launch = prepare_native_controls(
            task.selection,
            harness=_HARNESS_ID,
            catalog=self.catalog,
            probe_installed_host=self.installed_host,
        )
        workspace = str(task.workspace_path) if self.runner is None else WORKSPACE_MOUNT
        command = (
            self.config.executable,
            "--mode=json",
            "--no-session",
            "--cwd",
            workspace,
            "--max-time",
            str(task.time_budget_seconds),
            *self.resolved_permissions().arguments,
            *launch.arguments,
            "-p",
            _render_task(task),
        )
        return command, launch.observed_executable_version

    def container_environment(self, task: HarnessTask) -> tuple[tuple[str, str], ...]:
        """The explicit environment of the isolated worker; nothing is inherited from the host."""
        del task
        if self.runner is None:
            raise InvalidInputError("only an isolated OMP worker has a container environment")
        return worker_environment((("PI_CODING_AGENT_DIR", f"{WORKER_HOME}/.omp/agent"),))

    def _receipt(
        self,
        task: HarnessTask,
        events: tuple[NormalizedAdapterEvent, ...],
        launched_command: tuple[str, ...] | None,
        *,
        observed_at_launch: str | None,
        runner: RunnerReceiptRef | None = None,
    ) -> ExecutionReceipt:
        receipt = build_receipt(
            task.selection,
            harness=_HARNESS_ID,
            catalog=self.catalog,
            launched_command=launched_command,
            models=(event.attributes["model"] for event in events if "model" in event.attributes),
            observed_at_launch=observed_at_launch,
            runner=runner,
        )
        if runner is None:
            return receipt
        # An isolated worker is not trusted: the model id it printed is its own claim.
        return replace(receipt, effective_model=receipt.effective_model.worker_reported())

    def _catalog_host(self) -> InstalledHost:
        """The identity an isolated worker's runner receipt must record: the tested one."""
        host = self.catalog.host(_HARNESS_ID)
        return InstalledHost(
            executable_version=host.tested_executable_version,
            protocol_fingerprint=host.protocol_fingerprint,
        )

    def supervised_command(self, task: HarnessTask, *, result_path: Path) -> tuple[str, ...]:
        """Return a worker command whose parent is the coordinator supervisor."""
        if self.runner is not None:
            image_host = verified_image_host(self.catalog.host(_HARNESS_ID), self.installed_host())
            return isolated_command(
                runner=self.runner,
                harness=_HARNESS_ID,
                task=task,
                result_path=result_path,
                permissions=self.resolved_permissions(),
                environment=self.container_environment(task),
                inner_command=self.command_for(task),
                image_host=image_host,
                seed_files=relay_omp_seed_files(self.runner),
            )
        if not result_path.is_absolute():
            raise InvalidInputError("OMP worker result path must be absolute")
        command, observed = self._host_command(task)
        return (
            sys.executable,
            "-m",
            "enginery.engine.omp_worker",
            "--operation-id",
            str(task.operation_id),
            "--output",
            str(result_path),
            *(() if observed is None else (OBSERVED_HOST_FLAG, observed)),
            "--",
            *command,
        )

    def collect_supervised(
        self,
        task: HarnessTask,
        *,
        result_path: Path,
        launched_command: tuple[str, ...] | None = None,
    ) -> tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]:
        """Read a supervised worker handoff after its process exit is observed.

        An isolated worker's result file is untrusted: it is size-bounded, its output is
        redacted before it is published, and the attempt is only accepted together with the
        runner receipt the launcher wrote for it.
        """
        try:
            payload = json.loads(self._read_result(task, result_path))
        except json.JSONDecodeError as error:
            raise WorkerFailureError(
                "OMP supervised worker did not retain a valid result"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {
            "operation_id",
            "output",
            "schema_version",
            "terminal_status",
        }:
            raise WorkerFailureError("OMP supervised worker result has an invalid shape")
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
            raise WorkerFailureError("OMP supervised worker result has invalid field types")
        if schema_version != 1:
            raise WorkerFailureError("OMP supervised worker result schema version is unsupported")
        if operation_id != str(task.operation_id):
            raise WorkerFailureError("OMP supervised worker result operation does not match task")
        runner_ref: RunnerReceiptRef | None = None
        observed_at_launch = launch_observation(launched_command)
        if self.runner is not None:
            output = redact_credential_shaped_text(output)
            image_host = self._catalog_host()
            runner_ref = publish_runner_receipt(
                self.runner, task, self.artifact_store, expected_host=image_host
            )
            # The launcher recorded the harness it saw in the container, and the receipt
            # was just required to equal the tested identity, so that is the version
            # observed at launch; a host command carries it in the wrapper instead.
            observed_at_launch = image_host.executable_version
        result = OmpWorkerResult(
            operation_id=operation_id,
            terminal_status=terminal_status,
            output=output,
            schema_version=schema_version,
        )
        digest = self.artifact_store.publish_bytes(
            result.output.encode("utf-8"),
            media_type="application/json",
        )
        events = (
            _normalize_events(result.output, task.operation_id)
            if result.terminal_status == "succeeded"
            else ()
        )
        return (
            events,
            HarnessResult(
                session_id=f"omp-{task.operation_id}",
                terminal_status=result.terminal_status,
                outputs=(
                    HarnessOutput(
                        digest=digest,
                        redaction=RedactionClassification.SENSITIVE,
                    ),
                ),
                execution_receipt=self._receipt(
                    task,
                    events,
                    launched_command,
                    observed_at_launch=observed_at_launch,
                    runner=runner_ref,
                ),
            ),
        )

    def _read_result(self, task: HarnessTask, result_path: Path) -> str:
        if self.runner is not None:
            return read_attempt_result(self.runner, task, result_path, what="OMP supervised worker")
        try:
            return result_path.read_text(encoding="utf-8")
        except OSError as error:
            raise WorkerFailureError(
                "OMP supervised worker did not retain a valid result"
            ) from error

    def _require_session(self, session: HarnessSession) -> None:
        if self._sessions.get(session.session_id) != session:
            raise CancellationError("unknown OMP harness session")


_OMP_BANNER = re.compile(r"omp v(\S+)")
"""The exact first line: no whitespace is skipped, so a malformed banner yields no version."""


def probe_installed_host(
    executable: str,
    runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = (
        lambda arguments: subprocess.run(arguments, check=False, capture_output=True, text=True)
    ),
) -> InstalledHost | None:
    """Probe the installed OMP: its version is observed, its protocol is the one declared here.

    Needs no artifact store and creates nothing, so read-only diagnostics can use it. An
    executable that cannot be run or does not identify itself is unobservable, not an error.
    """
    try:
        completed = runner((executable, "--help"))
    except OSError:
        return None
    lines = completed.stdout.splitlines()
    banner = _OMP_BANNER.fullmatch(lines[0]) if lines else None
    if completed.returncode != 0 or banner is None:
        return None
    return InstalledHost(
        executable_version=banner.group(1),
        protocol_fingerprint=f"json-protocol={_OMP_PROTOCOL_VERSION}",
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
        attributes = {"protocol_version": str(_OMP_PROTOCOL_VERSION)}
        model = _assistant_model(payload)
        if model is not None:
            attributes["model"] = model
        events.append(
            NormalizedAdapterEvent(
                kind=kind,
                occurred_at=datetime.now(UTC),
                operation_id=operation_id,
                summary=f"OMP {event_type}",
                attributes=attributes,
            )
        )
    if not terminal_seen:
        raise WorkerFailureError("OMP output ended without an agent_end event")
    return tuple(events)


def _assistant_model(payload: dict[str, object]) -> str | None:
    """Return the provider-qualified model of a completed assistant message, if reported."""
    if payload.get("type") != "message_end":
        return None
    message = payload.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    provider = message.get("provider")
    model = message.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    if not provider.strip() or not model.strip():
        return None
    return f"{provider}/{model}"


__all__ = ["OmpAdapterConfig", "OmpHarness"]
