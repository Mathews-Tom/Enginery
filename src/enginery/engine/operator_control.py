"""Operator control: the durable pause flag, the selected runner image, and worker draining.

The operator's recovery entry point (``enginery operator``) is a fixed command that runs outside
any coding-agent session. This module is its engine side. The control state is one durable
process-manager record (``operator-control``/``control``) written together with an auditable
ledger event in a single atomic append, so a pause, a resume or a runner selection either
happens completely or not at all and is always attributable.

While the ledger is paused nothing new is dispatched: :func:`require_dispatch_allowed` is the
check every worker launch makes. Workers that are already running are never touched by a pause;
only an explicit drain waits for them or stops them through the existing supervisor path.

This module imports no workflow code and no runtime scheduler, so the runtime can depend on it.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from enginery.domain.errors import (
    EngineryError,
    HumanActionRequiredError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.engine.coordinator import Coordinator
from enginery.engine.supervisor import (
    Locality,
    Presence,
    ProcessIdentity,
    classify_locality,
    identity_from_state,
    probe_presence,
)
from enginery.engine.workspace import GitWorktreeBackend
from enginery.ledger.connection import open_connection
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.migrations import current_schema_version
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.schema import MIGRATIONS
from enginery.ledger.service import LedgerService
from enginery.ledger.verify import verify_ledger

OPERATOR_CONTROL_MANAGER = "operator-control"
CONTROL_STATE_KEY = "control"
CONTROL_AGGREGATE_TYPE = "operator_control"
CONTROL_AGGREGATE_ID = "control"
CONTROL_SCHEMA_VERSION = 1
PAUSED_MESSAGE = "dispatch is paused by the operator"

_SUPERVISOR_MANAGER = "worker-supervisor"
_ACTIVE_WORKER_STATUSES = frozenset({"launch_intended", "running", "termination_requested"})
_OPERATOR_OWNER = "enginery-operator"


@dataclass(frozen=True, slots=True)
class SelectedRunner:
    """The retained pinned runner image the operator selected, by digest, and what proved it."""

    image: str
    docker_context: str | None
    profile: str
    profile_digest: str
    engine: Mapping[str, object]
    image_identity: Mapping[str, object]
    selected_at: str

    def to_state(self) -> dict[str, object]:
        return {
            "image": self.image,
            "docker_context": self.docker_context,
            "profile": self.profile,
            "profile_digest": self.profile_digest,
            "engine": dict(self.engine),
            "image_identity": dict(self.image_identity),
            "selected_at": self.selected_at,
        }

    @classmethod
    def from_state(cls, state: object) -> SelectedRunner:
        data = _mapping(state, "selected runner")
        context = data.get("docker_context")
        if context is not None and not isinstance(context, str):
            raise InvalidInputError("selected runner docker context must be null or text")
        return cls(
            image=_text(data, "image"),
            docker_context=context,
            profile=_text(data, "profile"),
            profile_digest=_text(data, "profile_digest"),
            engine=_mapping(data.get("engine"), "selected runner engine"),
            image_identity=_mapping(data.get("image_identity"), "selected runner image"),
            selected_at=_text(data, "selected_at"),
        )


@dataclass(frozen=True, slots=True)
class ControlState:
    """The operator's durable control state; the default is running and no runner selected."""

    paused: bool = False
    changed_at: str | None = None
    reason: str | None = None
    selected_runner: SelectedRunner | None = None
    state_version: int = 0

    def to_report(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "changed_at": self.changed_at,
            "reason": self.reason,
            "selected_runner": (
                None if self.selected_runner is None else self.selected_runner.to_state()
            ),
        }


@dataclass(frozen=True, slots=True)
class ControlChange:
    """The control state after a command and whether the command changed anything."""

    state: ControlState
    changed: bool


def read_control_state(ledger: LedgerService) -> ControlState:
    record = ledger.read_process_manager_state(
        process_manager_name=OPERATOR_CONTROL_MANAGER, state_key=CONTROL_STATE_KEY
    )
    if record is None:
        return ControlState()
    state = record.state
    if state.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise InvalidInputError("operator control state schema version is unsupported")
    paused = state.get("paused")
    if not isinstance(paused, bool):
        raise InvalidInputError("operator control state paused flag must be a boolean")
    selected = state.get("selected_runner")
    return ControlState(
        paused=paused,
        changed_at=_optional_text(state, "changed_at"),
        reason=_optional_text(state, "reason"),
        selected_runner=None if selected is None else SelectedRunner.from_state(selected),
        state_version=record.state_version,
    )


def require_dispatch_allowed(ledger: LedgerService) -> None:
    """Refuse to launch a worker while the operator has paused dispatch."""
    if read_control_state(ledger).paused:
        raise HumanActionRequiredError(PAUSED_MESSAGE)


class OperatorControl:
    """Write the operator's pause flag and runner selection as atomic, auditable ledger events."""

    def __init__(self, ledger: LedgerService) -> None:
        self._ledger = ledger

    def state(self) -> ControlState:
        return read_control_state(self._ledger)

    def pause(self, *, now: datetime, reason: str | None = None) -> ControlChange:
        """Pause dispatch. Pausing a paused ledger changes nothing."""
        previous = self.state()
        if previous.paused:
            return ControlChange(previous, changed=False)
        return self._write(
            previous,
            event_type="operator.paused",
            now=now,
            paused=True,
            reason=_clean_reason(reason),
            selected_runner=previous.selected_runner,
        )

    def resume(self, *, now: datetime) -> ControlChange:
        """Resume dispatch. Resuming a running ledger changes nothing."""
        previous = self.state()
        if not previous.paused:
            return ControlChange(previous, changed=False)
        return self._write(
            previous,
            event_type="operator.resumed",
            now=now,
            paused=False,
            reason=None,
            selected_runner=previous.selected_runner,
        )

    def select_runner(self, runner: SelectedRunner, *, now: datetime) -> ControlChange:
        """Record the qualified pinned runner image. Reselecting the same one changes nothing."""
        previous = self.state()
        current = previous.selected_runner
        if current is not None and (current.image, current.docker_context, current.profile) == (
            runner.image,
            runner.docker_context,
            runner.profile,
        ):
            return ControlChange(previous, changed=False)
        return self._write(
            previous,
            event_type="operator.runner_selected",
            now=now,
            paused=previous.paused,
            reason=previous.reason,
            selected_runner=runner,
        )

    def _write(
        self,
        previous: ControlState,
        *,
        event_type: str,
        now: datetime,
        paused: bool,
        reason: str | None,
        selected_runner: SelectedRunner | None,
    ) -> ControlChange:
        if now.tzinfo is None:
            raise InvalidInputError("operator control time must be timezone-aware")
        state: dict[str, object] = {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "paused": paused,
            "changed_at": now.isoformat(),
            "reason": reason,
            "selected_runner": None if selected_runner is None else selected_runner.to_state(),
        }
        projection = self._ledger.read_projection(
            aggregate_type=CONTROL_AGGREGATE_TYPE, aggregate_id=CONTROL_AGGREGATE_ID
        )
        result = self._ledger.append(
            AppendCommand(
                correlation_id=f"operator-control:{previous.state_version + 1}:{event_type}",
                events=(
                    EventWrite(
                        aggregate_type=CONTROL_AGGREGATE_TYPE,
                        aggregate_id=CONTROL_AGGREGATE_ID,
                        expected_version=0 if projection is None else projection.aggregate_version,
                        event_type=event_type,
                        schema_version=CONTROL_SCHEMA_VERSION,
                        payload=state,
                    ),
                ),
                process_manager_updates=(
                    ProcessManagerStateWrite(
                        OPERATOR_CONTROL_MANAGER,
                        CONTROL_STATE_KEY,
                        previous.state_version,
                        state,
                    ),
                ),
            )
        )
        return ControlChange(
            ControlState(
                paused=paused,
                changed_at=now.isoformat(),
                reason=reason,
                selected_runner=selected_runner,
                state_version=result.process_manager_states[0].state_version,
            ),
            changed=True,
        )


def open_operator_ledger(database: Path) -> LedgerService:
    """Open an existing, current-schema ledger without creating or migrating anything."""
    if not database.is_file():
        raise MissingPrerequisiteError(f"ledger {database} does not exist")
    try:
        connection = open_connection(database)
    except sqlite3.DatabaseError as error:
        raise MissingPrerequisiteError(f"ledger {database} cannot be opened: {error}") from error
    try:
        version = current_schema_version(connection)
    except sqlite3.DatabaseError as error:
        connection.close()
        raise MissingPrerequisiteError(f"ledger {database} cannot be read: {error}") from error
    if version != MIGRATIONS[-1].version:
        connection.close()
        raise MissingPrerequisiteError(
            f"ledger {database} is at schema version {version}, expected {MIGRATIONS[-1].version}; "
            "migrate it before using the operator commands"
        )
    return LedgerService(connection)


@dataclass(frozen=True, slots=True)
class WorkerView:
    """One supervised worker that has not been reconciled, and what its process shows now."""

    run_id: str
    node_id: str
    attempt_id: str
    operation_id: str
    status: str
    identity: ProcessIdentity | None
    process: str
    live: bool

    def to_state(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "operation_id": self.operation_id,
            "status": self.status,
            "pid": None if self.identity is None else self.identity.pid,
            "process_group_id": None if self.identity is None else self.identity.process_group_id,
            "process": self.process,
            "live": self.live,
        }


def list_workers(ledger: LedgerService) -> tuple[WorkerView, ...]:
    """Every supervised worker whose exit has not been observed, with its process presence."""
    views: list[WorkerView] = []
    for record in ledger.list_process_manager_states(process_manager_name=_SUPERVISOR_MANAGER):
        status = str(record.state.get("status"))
        if status not in _ACTIVE_WORKER_STATUSES:
            continue
        identity: ProcessIdentity | None = None
        process = "no-process-recorded"
        live = True
        if status != "launch_intended":
            try:
                identity = identity_from_state(record.state)
            except EngineryError:
                process = "identity-unreadable"
            else:
                process, live = _observe_process(identity)
        views.append(
            WorkerView(
                run_id=str(record.state.get("run_id")),
                node_id=str(record.state.get("node_id")),
                attempt_id=str(record.state.get("attempt_id")),
                operation_id=str(record.state.get("operation_id")),
                status=status,
                identity=identity,
                process=process,
                live=live,
            )
        )
    return tuple(views)


def live_workers(ledger: LedgerService) -> tuple[WorkerView, ...]:
    """The workers that are, or cannot be proven not to be, running."""
    return tuple(view for view in list_workers(ledger) if view.live)


def _observe_process(identity: ProcessIdentity) -> tuple[str, bool]:
    """Describe the recorded process. Anything not provably gone counts as live."""
    locality = classify_locality(identity)
    if locality is Locality.FOREIGN:
        return "foreign-host", True
    if locality is Locality.REBOOTED:
        return "gone-after-reboot", False
    with contextlib.suppress(ChildProcessError):
        os.waitpid(identity.pid, os.WNOHANG)
    presence = probe_presence(identity)
    if presence is Presence.SAME:
        return "present", True
    if presence is Presence.UNPROVABLE:
        return "unprovable", True
    if _group_has_members(identity.process_group_id):
        return "group-members-present", True
    return ("absent" if presence is Presence.ABSENT else "pid-reused"), False


def _group_has_members(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # held by another user's processes: not this worker's group
    return True


@dataclass(frozen=True, slots=True)
class StopResult:
    """The outcome of stopping one worker through the supervisor cancel path."""

    worker: WorkerView
    stopped: bool
    error_class: str | None = None
    error: str | None = None
    containers_removed: tuple[str, ...] = ()
    container_error: str | None = None

    def to_state(self) -> dict[str, object]:
        return {
            "run_id": self.worker.run_id,
            "node_id": self.worker.node_id,
            "operation_id": self.worker.operation_id,
            "stopped": self.stopped,
            "error_class": self.error_class,
            "error": self.error,
            "containers_removed": list(self.containers_removed),
            "container_error": self.container_error,
        }


@dataclass(frozen=True, slots=True)
class DrainReport:
    """What a drain did and what is still running afterwards."""

    mode: str
    control: ControlState
    waited_seconds: float
    before: tuple[WorkerView, ...]
    stopped: tuple[StopResult, ...]
    remaining: tuple[WorkerView, ...]

    @property
    def drained(self) -> bool:
        return not self.remaining

    @property
    def stop_failures(self) -> tuple[StopResult, ...]:
        return tuple(
            result
            for result in self.stopped
            if not result.stopped or result.container_error is not None
        )

    def to_state(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "paused": self.control.paused,
            "drained": self.drained,
            "waited_seconds": round(self.waited_seconds, 3),
            "workers_before": [view.to_state() for view in self.before],
            "stopped": [result.to_state() for result in self.stopped],
            "remaining": [view.to_state() for view in self.remaining],
        }


StopWorker = Callable[[WorkerView], None]
"""Stop one live worker through the supervisor cancel path; raise on failure."""

RemoveContainers = Callable[[WorkerView], tuple[str, ...]]
"""Remove the isolated worker container(s) of one stopped worker; return their names."""


def drain_wait(
    ledger: LedgerService,
    *,
    now: datetime,
    wait_seconds: float,
    poll_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DrainReport:
    """Pause, then wait up to ``wait_seconds`` for every running worker to exit on its own."""
    if wait_seconds < 0:
        raise InvalidInputError("the drain wait cannot be negative")
    control = OperatorControl(ledger).pause(now=now, reason="drain").state
    before = live_workers(ledger)
    started = monotonic()
    remaining = before
    while remaining:
        elapsed = monotonic() - started
        if elapsed >= wait_seconds:
            break
        sleep(min(poll_seconds, wait_seconds - elapsed))
        remaining = live_workers(ledger)
    return DrainReport(
        mode="wait",
        control=control,
        waited_seconds=monotonic() - started,
        before=before,
        stopped=(),
        remaining=remaining,
    )


def drain_stop(
    ledger: LedgerService,
    *,
    now: datetime,
    stop_worker: StopWorker,
    remove_containers: RemoveContainers | None = None,
) -> DrainReport:
    """Pause, then stop every running worker through the existing supervisor cancel path.

    Each worker is stopped independently; a worker that cannot be stopped is reported with its
    failure and the rest are still stopped. ``remove_containers``, when given, then removes the
    isolated worker container of each stopped worker, so no container outlives its worker.
    """
    control = OperatorControl(ledger).pause(now=now, reason="drain").state
    before = live_workers(ledger)
    started = time.monotonic()
    results: list[StopResult] = []
    for worker in before:
        try:
            stop_worker(worker)
        except EngineryError as error:
            results.append(
                StopResult(
                    worker, stopped=False, error_class=error.failure_class.value, error=str(error)
                )
            )
            continue
        removed: tuple[str, ...] = ()
        container_error: str | None = None
        if remove_containers is not None:
            try:
                removed = remove_containers(worker)
            except EngineryError as error:
                container_error = str(error)
        results.append(
            StopResult(
                worker, stopped=True, containers_removed=removed, container_error=container_error
            )
        )
    return DrainReport(
        mode="stop",
        control=control,
        waited_seconds=time.monotonic() - started,
        before=before,
        stopped=tuple(results),
        remaining=live_workers(ledger),
    )


def operator_status(
    ledger: LedgerService, *, database: Path, artifacts: Path | None
) -> dict[str, object]:
    """The operator's status report: control state, workers, workspaces and ledger health."""
    workers = list_workers(ledger)
    reservations = GitWorktreeBackend(
        ledger, Coordinator(ledger, owner=_OPERATOR_OWNER)
    ).list_reservations()
    by_status: dict[str, int] = {}
    for reservation in reservations:
        by_status[reservation.status] = by_status.get(reservation.status, 0) + 1
    verification = verify_ledger(database, artifact_store_root=artifacts)
    return {
        "database": str(database),
        **read_control_state(ledger).to_report(),
        "workers": [view.to_state() for view in workers],
        "running_workers": sum(1 for view in workers if view.live),
        "retained_workspaces": by_status.get("retained", 0),
        "workspaces_by_status": dict(sorted(by_status.items())),
        "ledger": {
            "healthy": verification.healthy,
            "schema_version": verification.schema_version,
            "issues": [
                {"code": issue.code, "detail": issue.detail} for issue in verification.issues
            ],
        },
    }


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    reason = reason.strip()
    if not reason or len(reason) > 500 or "\n" in reason:
        raise InvalidInputError("a pause reason must be one line of at most 500 characters")
    return reason


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidInputError(f"{name} must be a mapping")
    return value


def _text(state: Mapping[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"operator control state {key} must be non-blank text")
    return value


def _optional_text(state: Mapping[str, object], key: str) -> str | None:
    value = state.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInputError(f"operator control state {key} must be null or text")
    return value


__all__ = [
    "CONTROL_STATE_KEY",
    "OPERATOR_CONTROL_MANAGER",
    "PAUSED_MESSAGE",
    "ControlChange",
    "ControlState",
    "DrainReport",
    "OperatorControl",
    "RemoveContainers",
    "SelectedRunner",
    "StopResult",
    "StopWorker",
    "WorkerView",
    "drain_stop",
    "drain_wait",
    "list_workers",
    "live_workers",
    "open_operator_ledger",
    "operator_status",
    "read_control_state",
    "require_dispatch_allowed",
]
