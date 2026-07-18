"""Independent process-group supervision for leased workers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

_MANAGER = "worker-supervisor"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    process_group_id: int
    start_identity: str


class WorkerSupervisor:
    """Launch and terminate complete worker process groups under a lease."""

    def __init__(self, ledger: LedgerService, coordinator: Coordinator) -> None:
        self._ledger = ledger
        self._coordinator = coordinator

    def start(
        self, *, lease: FencedNodeLease, command: tuple[str, ...], cwd: Path, now: datetime
    ) -> ProcessIdentity:
        if not command:
            raise InvalidInputError("worker command must not be empty")
        process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
        identity = probe_process(process.pid)
        if identity is None:
            raise InternalInvariantViolationError(
                "started worker disappeared before identity capture"
            )
        state = _state(lease, identity)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"worker-start:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, "worker.started", state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(_MANAGER, _key(lease), 0, state),
                ),
            )
        )
        return identity

    def cancel(self, *, lease: FencedNodeLease, identity: ProcessIdentity, now: datetime) -> None:
        current = probe_process(identity.pid)
        if current is not None and current != identity:
            raise ExternalConflictError("process identity changed; refusing PID-reuse termination")
        if current is not None:
            os.killpg(identity.process_group_id, signal.SIGTERM)
            try:
                os.waitpid(identity.pid, 0)
            except ChildProcessError as error:
                raise InternalInvariantViolationError(
                    "supervised worker was reaped without an observed exit"
                ) from error
        self._record_exit(lease=lease, identity=identity, now=now, event_type="worker.cancelled")

    def enforce_heartbeat(
        self, *, lease: FencedNodeLease, identity: ProcessIdentity, now: datetime
    ) -> bool:
        epoch = self._coordinator.current_epoch()
        if epoch is not None and epoch.epoch == lease.epoch and epoch.active_at(now):
            return False
        current = probe_process(identity.pid)
        if current is not None and current != identity:
            raise ExternalConflictError("process identity changed; refusing PID-reuse termination")
        if current is not None:
            os.killpg(identity.process_group_id, signal.SIGTERM)
        return True

    def _record_exit(
        self, *, lease: FencedNodeLease, identity: ProcessIdentity, now: datetime, event_type: str
    ) -> None:
        state = _state(lease, identity, status="exited")
        record = self._ledger.read_process_manager_state(
            process_manager_name=_MANAGER, state_key=_key(lease)
        )
        if record is None:
            raise InternalInvariantViolationError("supervisor state missing for leased worker")
        self._ledger.append(
            AppendCommand(
                correlation_id=f"{event_type}:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, event_type, state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                    ProcessManagerStateWrite(_MANAGER, _key(lease), record.state_version, state),
                ),
            )
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
            ["ps", "-o", "lstart=", "-p", str(pid)], text=True, capture_output=True, check=False
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


def _key(lease: FencedNodeLease) -> str:
    return f"{lease.run_id}:{lease.node_id}"


def _state(
    lease: FencedNodeLease, identity: ProcessIdentity, status: str = "running"
) -> dict[str, object]:
    return {
        "run_id": lease.run_id,
        "node_id": lease.node_id,
        "attempt_id": lease.attempt_id,
        "epoch": lease.epoch,
        "fencing_token": lease.fencing_token,
        "pid": identity.pid,
        "process_group_id": identity.process_group_id,
        "start_identity": identity.start_identity,
        "status": status,
    }


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
