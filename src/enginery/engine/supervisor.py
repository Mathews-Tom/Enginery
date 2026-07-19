"""Independent process-group supervision for leased workers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease
from enginery.ledger.events import AppendCommand, AppendResult, EventWrite
from enginery.ledger.process_manager import (
    ProcessManagerStateRecord,
    ProcessManagerStateWrite,
)
from enginery.ledger.service import LedgerService

_MANAGER = "worker-supervisor"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    process_group_id: int
    start_identity: str


class WorkerSupervisor:
    """Launch and terminate complete worker process groups under a lease."""

    def __init__(
        self,
        ledger: LedgerService,
        coordinator: Coordinator,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._coordinator = coordinator
        self._fault_hook = fault_hook

    def start(
        self,
        *,
        lease: FencedNodeLease,
        command: tuple[str, ...],
        cwd: Path,
        now: datetime,
    ) -> ProcessIdentity:
        """Persist launch intent before spawning so crash recovery fails closed."""
        if not command:
            raise InvalidInputError("worker command must not be empty")
        prior = self._ledger.read_process_manager_state(
            process_manager_name=_MANAGER,
            state_key=_key(lease),
        )
        if prior is not None and prior.state.get("status") != "exit_reconciled":
            raise ExternalConflictError(
                "prior worker supervision has not been reconciled for replacement"
            )
        expected_state_version = 0 if prior is None else prior.state_version
        launch_state = _state(lease, identity=None, status="launch_intended")
        launch_result = self._ledger.append(
            AppendCommand(
                correlation_id=f"worker-launch-intent:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, "worker.launch_intended", launch_state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(
                        _MANAGER,
                        _key(lease),
                        expected_state_version,
                        launch_state,
                    ),
                ),
            )
        )
        self._fault("worker_launch_intended")
        try:
            process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
        except OSError as error:
            self._record_launch_failure(
                lease=lease,
                state_version=launch_result.process_manager_states[1].state_version,
                now=now,
                error=error,
            )
            raise ExternalConflictError("worker process launch failed") from error
        self._fault("worker_process_started")
        identity = probe_process(process.pid)
        if identity is None:
            raise InternalInvariantViolationError(
                "started worker disappeared before identity capture; human reconciliation required"
            )
        self._record_running(
            lease=lease,
            identity=identity,
            state_version=launch_result.process_manager_states[1].state_version,
            now=now,
        )
        self._fault("worker_identity_persisted")
        return identity

    def _record_running(
        self,
        *,
        lease: FencedNodeLease,
        identity: ProcessIdentity,
        state_version: int,
        now: datetime,
    ) -> None:
        state = _state(lease, identity=identity, status="running")
        self._ledger.append(
            AppendCommand(
                correlation_id=f"worker-start:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, "worker.started", state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(_MANAGER, _key(lease), state_version, state),
                ),
            )
        )

    def _record_launch_failure(
        self,
        *,
        lease: FencedNodeLease,
        state_version: int,
        now: datetime,
        error: OSError,
    ) -> None:
        state = _state(lease, identity=None, status="launch_failed")
        state["error"] = str(error)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"worker-launch-failed:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, "worker.launch_failed", state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(_MANAGER, _key(lease), state_version, state),
                ),
            )
        )

    def cancel(self, *, lease: FencedNodeLease, identity: ProcessIdentity, now: datetime) -> None:
        """Request, observe, and durably record complete process-group termination."""
        record = self._require_supervisor_record(lease)
        requested = _state(lease, identity=identity, status="termination_requested")
        requested_result = self._ledger.append(
            AppendCommand(
                correlation_id=f"worker-termination:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, "worker.termination_requested", requested),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(
                        _MANAGER,
                        _key(lease),
                        record.state_version,
                        requested,
                    ),
                ),
            )
        )
        self._fault("termination_requested")
        _terminate_exact_group(identity)
        self._fault("process_exit_observed")
        self._record_exit(
            lease=lease,
            identity=identity,
            now=now,
            event_type="worker.cancelled",
            expected_state_version=requested_result.process_manager_states[1].state_version,
        )

    def cancel_persisted(self, *, lease: FencedNodeLease, now: datetime) -> None:
        """Terminate the current worker using only its durable supervisor record."""
        record = self._require_supervisor_record(lease)
        if record.state.get("status") != "running":
            raise ExternalConflictError("only a running worker can be cancelled")
        self.cancel(
            lease=lease,
            identity=_identity_from_state(record),
            now=now,
        )

    def enforce_heartbeat(
        self, *, lease: FencedNodeLease, identity: ProcessIdentity, now: datetime
    ) -> bool:
        """Monitor coordinator expiry without mutating workflow aggregates."""
        epoch = self._coordinator.current_epoch()
        if epoch is not None and epoch.epoch == lease.epoch and epoch.active_at(now):
            return False
        record = self._require_supervisor_record(lease)
        requested = _state(lease, identity=identity, status="termination_requested")
        requested["reason"] = "coordinator_heartbeat_expired"
        requested_result = self._append_supervisor_observation(
            lease=lease,
            event_type="supervisor.termination_requested",
            state=requested,
            expected_state_version=record.state_version,
        )
        self._fault("termination_requested")
        _terminate_exact_group(identity)
        self._fault("process_exit_observed")
        observed = _state(lease, identity=identity, status="exit_observed")
        observed["reason"] = "coordinator_heartbeat_expired"
        self._append_supervisor_observation(
            lease=lease,
            event_type="supervisor.process_exit_observed",
            state=observed,
            expected_state_version=requested_result.process_manager_states[0].state_version,
        )
        return True

    def observe_exit(self, *, lease: FencedNodeLease, now: datetime) -> None:
        """Record a naturally exited worker after exact identity re-probing."""
        record = self._require_supervisor_record(lease)
        if record.state.get("status") == "exit_observed":
            return
        identity = _identity_from_state(record)
        try:
            reaped_pid, _ = os.waitpid(identity.pid, os.WNOHANG)
        except ChildProcessError:
            reaped_pid = 0
        if reaped_pid == identity.pid:
            self._fault("process_exit_observed")
            self._record_exit(
                lease=lease,
                identity=identity,
                now=now,
                event_type="worker.process_exit_observed",
                expected_state_version=record.state_version,
            )
            return
        observed = probe_process(identity.pid)
        if observed is not None:
            if observed != identity:
                raise ExternalConflictError(
                    "process identity changed; refusing result after PID reuse"
                )
            raise ExternalConflictError(
                "worker result arrived before supervised process exit was observed"
            )
        self._fault("process_exit_observed")
        self._record_exit(
            lease=lease,
            identity=identity,
            now=now,
            event_type="worker.process_exit_observed",
            expected_state_version=record.state_version,
        )

    def _record_exit(
        self,
        *,
        lease: FencedNodeLease,
        identity: ProcessIdentity,
        now: datetime,
        event_type: str,
        expected_state_version: int,
    ) -> None:
        state = _state(lease, identity=identity, status="exit_observed")
        self._ledger.append(
            AppendCommand(
                correlation_id=f"{event_type}:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, event_type, state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(_MANAGER, _key(lease), expected_state_version, state),
                ),
            )
        )

    def _require_supervisor_record(self, lease: FencedNodeLease) -> ProcessManagerStateRecord:
        record = self._ledger.read_process_manager_state(
            process_manager_name=_MANAGER, state_key=_key(lease)
        )
        if record is None:
            raise InternalInvariantViolationError("supervisor state missing for leased worker")
        return record

    def _append_supervisor_observation(
        self,
        *,
        lease: FencedNodeLease,
        event_type: str,
        state: dict[str, object],
        expected_state_version: int,
    ) -> AppendResult:
        projection = self._ledger.read_projection(
            aggregate_type="supervisor", aggregate_id=_key(lease)
        )
        return self._ledger.append(
            AppendCommand(
                correlation_id=f"{event_type}:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(
                    EventWrite(
                        aggregate_type="supervisor",
                        aggregate_id=_key(lease),
                        expected_version=0 if projection is None else projection.aggregate_version,
                        event_type=event_type,
                        schema_version=1,
                        payload=state,
                    ),
                ),
                process_manager_updates=(
                    ProcessManagerStateWrite(_MANAGER, _key(lease), expected_state_version, state),
                ),
            )
        )

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)


def _terminate_exact_group(identity: ProcessIdentity) -> None:
    current = probe_process(identity.pid)
    if current is not None and current != identity:
        raise ExternalConflictError("process identity changed; refusing PID-reuse termination")
    if current is None:
        return
    os.killpg(identity.process_group_id, signal.SIGTERM)
    try:
        waited_pid, _ = os.waitpid(identity.pid, 0)
    except ChildProcessError as error:
        raise InternalInvariantViolationError(
            "worker exit cannot be observed by this supervisor; human reconciliation required"
        ) from error
    if waited_pid != identity.pid:
        raise InternalInvariantViolationError(
            "worker supervisor observed an unexpected process exit"
        )
    if probe_process(identity.pid) is not None:
        raise InternalInvariantViolationError(
            "worker process remains present after process-group termination"
        )


def probe_process(pid: int) -> ProcessIdentity | None:
    if pid < 1:
        raise InvalidInputError("pid must be positive")
    try:
        process_group_id = os.getpgid(pid)
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=False,
        )
        start_identity = completed.stdout.strip()
    elif sys.platform.startswith("linux"):
        try:
            start_identity = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
        except (FileNotFoundError, IndexError) as error:
            raise InternalInvariantViolationError(
                "cannot establish Linux process-start identity"
            ) from error
    else:
        raise InvalidInputError("worker supervision supports only macOS and Linux")
    if not start_identity:
        raise InternalInvariantViolationError("process-start identity was empty")
    return ProcessIdentity(pid, process_group_id, start_identity)


def _identity_from_state(record: ProcessManagerStateRecord) -> ProcessIdentity:
    state = record.state
    pid = state.get("pid")
    process_group_id = state.get("process_group_id")
    start_identity = state.get("start_identity")
    if (
        not isinstance(pid, int)
        or not isinstance(process_group_id, int)
        or not isinstance(start_identity, str)
        or not start_identity
    ):
        raise InternalInvariantViolationError("stored supervisor process identity is invalid")
    return ProcessIdentity(pid, process_group_id, start_identity)


def _key(lease: FencedNodeLease) -> str:
    return f"{lease.run_id}:{lease.node_id}"


def _state(
    lease: FencedNodeLease, *, identity: ProcessIdentity | None, status: str
) -> dict[str, object]:
    state: dict[str, object] = {
        "run_id": lease.run_id,
        "node_id": lease.node_id,
        "attempt_id": lease.attempt_id,
        "epoch": lease.epoch,
        "fencing_token": lease.fencing_token,
        "operation_id": lease.operation_id,
        "status": status,
    }
    if identity is not None:
        state.update(
            {
                "pid": identity.pid,
                "process_group_id": identity.process_group_id,
                "start_identity": identity.start_identity,
            }
        )
    return state


def _event(
    ledger: LedgerService, lease: FencedNodeLease, event_type: str, payload: dict[str, object]
) -> EventWrite:
    aggregate_id = _key(lease)
    projection = ledger.read_projection(aggregate_type="worker", aggregate_id=aggregate_id)
    return EventWrite(
        "worker",
        aggregate_id,
        0 if projection is None else projection.aggregate_version,
        event_type,
        1,
        payload,
    )


__all__ = ["ProcessIdentity", "WorkerSupervisor", "probe_process"]
