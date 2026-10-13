"""Independent process-group supervision for leased workers."""

from __future__ import annotations

import contextlib
import enum
import functools
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

from enginery.domain.errors import (
    ExternalConflictError,
    HumanActionRequiredError,
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


IDENTITY_VERSION = 2
"""Schema version of a persisted worker identity that is bound to its host.

A record persisted before host binding carries no version and decodes as a legacy identity
(``host is None``). It is compared under single-host semantics -- the only semantics that
existed when it was written -- so an existing run is never stranded by the upgrade. Every new
record binds the host and boot it was launched on.
"""


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """The machine and boot a process belongs to: a pid is only meaningful within both.

    Only ``machine_id`` and ``boot_id`` are compared; ``hostname`` is display text.
    """

    machine_id: str
    boot_id: str
    hostname: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    process_group_id: int
    start_identity: str
    host: HostIdentity | None = None

    @property
    def legacy(self) -> bool:
        """True for an identity persisted before host binding."""
        return self.host is None


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
        stderr_log: Path | None = None,
    ) -> ProcessIdentity:
        """Persist launch intent before spawning so crash recovery fails closed."""
        if not command:
            raise InvalidInputError("worker command must not be empty")
        probe_host()  # fail closed before launch if this host cannot be identified
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
            # The worker must not hold the coordinator's stdio: an operator piping a
            # lifecycle command to a reader would otherwise block until the worker exits.
            # Its output is captured by the adapter's protocol file; only the wrapper's own
            # stderr (for example a crash trace) goes to a per-attempt log when one is given.
            stderr: int | IO[bytes] = subprocess.DEVNULL
            log_handle = None
            if stderr_log is not None:
                stderr_log.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(stderr_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                log_handle = os.fdopen(descriptor, "ab")
                stderr = log_handle
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                )
            finally:
                if log_handle is not None:
                    log_handle.close()
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

    def identity_for(self, *, lease: FencedNodeLease) -> ProcessIdentity:
        """Read the persisted identity for a worker being reconciled."""
        return identity_from_state(self._require_supervisor_record(lease).state)

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

    def cancel(
        self,
        *,
        lease: FencedNodeLease,
        identity: ProcessIdentity,
        now: datetime,
        coordinator_epoch: int | None = None,
    ) -> None:
        """Request, observe, and durably record complete process-group termination.

        The transition is fenced by ``coordinator_epoch``, which defaults to the epoch that
        issued the lease and must be the current epoch when a later coordinator cancels.
        Safe to retry after an interruption: a recorded termination request resumes at the
        exact-identity termination (which returns if the group is already gone), and an
        already observed exit is left as recorded.
        """
        locality = classify_locality(identity)
        if locality is Locality.FOREIGN:
            require_local(identity, action="terminate")
        epoch = lease.epoch if coordinator_epoch is None else coordinator_epoch
        record = self._require_supervisor_record(lease)
        status = record.state.get("status")
        if status in {"exit_observed", "exit_reconciled"}:
            return
        if status == "running":
            requested = _state(lease, identity=identity, status="termination_requested")
            requested_result = self._ledger.append(
                AppendCommand(
                    correlation_id=(
                        f"worker-termination:{lease.run_id}:{lease.node_id}:{lease.fencing_token}"
                    ),
                    events=(
                        _event(self._ledger, lease, "worker.termination_requested", requested),
                    ),
                    process_manager_updates=(
                        self._coordinator.epoch_guard(epoch=epoch, now=now),
                        ProcessManagerStateWrite(
                            _MANAGER, _key(lease), record.state_version, requested
                        ),
                    ),
                )
            )
            state_version = requested_result.process_manager_states[1].state_version
            self._fault("termination_requested")
        elif status == "termination_requested":
            state_version = record.state_version
        else:
            raise ExternalConflictError(
                "only a running or terminating worker can be cancelled",
                details={"status": str(status)},
            )
        # A rebooted host cannot still be running the worker, so nothing is signalled.
        reason = (
            "worker_lost_to_reboot"
            if locality is Locality.REBOOTED
            else _terminate_exact_group(identity)
        )
        self._fault("process_exit_observed")
        self._record_exit(
            lease=lease,
            coordinator_epoch=epoch,
            identity=identity,
            now=now,
            event_type="worker.cancelled",
            expected_state_version=state_version,
            reason=reason,
        )

    def cancel_persisted(
        self, *, lease: FencedNodeLease, now: datetime, coordinator_epoch: int | None = None
    ) -> None:
        """Terminate the current worker using only its durable supervisor record."""
        record = self._require_supervisor_record(lease)
        self.cancel(
            lease=lease,
            identity=identity_from_state(record.state),
            now=now,
            coordinator_epoch=coordinator_epoch,
        )

    def enforce_heartbeat(
        self, *, lease: FencedNodeLease, identity: ProcessIdentity, now: datetime
    ) -> bool:
        """Monitor coordinator expiry without mutating workflow aggregates."""
        epoch = self._coordinator.current_epoch()
        if epoch is not None and epoch.epoch == lease.epoch and epoch.active_at(now):
            return False
        require_local(identity, action="terminate")
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

    def observe_exit(
        self, *, lease: FencedNodeLease, coordinator_epoch: int, now: datetime
    ) -> None:
        """Record that a worker is no longer running, without ever signalling it.

        A naturally exited worker is confirmed by exact identity re-probing. A worker whose
        pid is now held by a different process, or whose host rebooted since it launched,
        cannot still be running: that is recorded as an inferred exit with its reason. A
        worker recorded on a different machine proves nothing and is refused. Repeating the
        call after an exit was recorded changes nothing.
        """
        record = self._require_supervisor_record(lease)
        if record.state.get("status") in {"exit_observed", "exit_reconciled"}:
            return
        identity = identity_from_state(record.state)
        locality = classify_locality(identity)
        if locality is Locality.FOREIGN:
            require_local(identity, action="observe")
        if locality is Locality.REBOOTED:
            self._record_inferred_exit(
                lease, record, identity, coordinator_epoch, now, "worker_lost_to_reboot"
            )
            return
        try:
            reaped_pid, _ = os.waitpid(identity.pid, os.WNOHANG)
        except ChildProcessError:
            reaped_pid = 0
        if reaped_pid != identity.pid:
            try:
                observed = probe_like(identity)
            except PermissionError:
                self._record_inferred_exit(
                    lease, record, identity, coordinator_epoch, now, MISMATCH
                )
                return
            if observed is not None:
                if not same_process(identity, observed):
                    self._record_inferred_exit(
                        lease, record, identity, coordinator_epoch, now, MISMATCH
                    )
                    return
                raise ExternalConflictError(
                    "worker result arrived before supervised process exit was observed"
                )
        self._fault("process_exit_observed")
        self._record_exit(
            lease=lease,
            coordinator_epoch=coordinator_epoch,
            identity=identity,
            now=now,
            event_type="worker.process_exit_observed",
            expected_state_version=record.state_version,
        )

    def _record_inferred_exit(
        self,
        lease: FencedNodeLease,
        record: ProcessManagerStateRecord,
        identity: ProcessIdentity,
        coordinator_epoch: int,
        now: datetime,
        reason: str,
    ) -> None:
        self._fault("process_exit_observed")
        self._record_exit(
            lease=lease,
            coordinator_epoch=coordinator_epoch,
            identity=identity,
            now=now,
            event_type="worker.exit_inferred",
            expected_state_version=record.state_version,
            reason=reason,
        )

    def _record_exit(
        self,
        *,
        lease: FencedNodeLease,
        coordinator_epoch: int,
        identity: ProcessIdentity,
        now: datetime,
        event_type: str,
        expected_state_version: int,
        reason: str | None = None,
    ) -> None:
        state = _state(lease, identity=identity, status="exit_observed")
        if reason is not None:
            state["exit_reason"] = reason
        self._ledger.append(
            AppendCommand(
                correlation_id=f"{event_type}:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(_event(self._ledger, lease, event_type, state),),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=coordinator_epoch, now=now),
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


_TERMINATION_GRACE_SECONDS = 10.0
_EXIT_POLL_SECONDS = 0.02


MISMATCH = "start_identity_mismatch"


def _terminate_exact_group(identity: ProcessIdentity) -> str | None:
    """Terminate the recorded worker's whole process group within a bounded time.

    The worker may have been launched by an earlier coordinator process, so its exit cannot
    be observed with ``waitpid`` alone: it is reaped when it is this process's child and
    otherwise observed by re-probing its exact start identity. The group is only treated as
    gone when the leader and every member have exited, so a leader that died while a
    descendant kept running is still terminated. SIGTERM gets a grace period, then SIGKILL; a
    group that survives both is reported instead of waited on forever. Safe to repeat.

    When the pid is now held by a different process (a different start identity, or one this
    user cannot signal) the recorded worker is provably gone and nothing may be signalled:
    ``MISMATCH`` is returned so the caller records it as evidence.
    """
    require_local(identity, action="terminate")
    try:
        current = probe_like(identity)
    except PermissionError:
        return MISMATCH
    if current is not None and not same_process(identity, current):
        return MISMATCH
    if current is None and _group_is_empty(identity.process_group_id):
        return None
    for termination in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(identity.process_group_id, termination)
        if _await_group_exit(identity, timeout=_TERMINATION_GRACE_SECONDS):
            return None
    raise InternalInvariantViolationError(
        "worker process group remains present after termination; human reconciliation required"
    )


def _await_group_exit(identity: ProcessIdentity, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(identity.pid, os.WNOHANG)
        try:
            observed = probe_like(identity)
        except PermissionError:
            observed = None  # held by another user's process: no longer our worker
        if (observed is None or not same_process(identity, observed)) and _group_is_empty(
            identity.process_group_id
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_EXIT_POLL_SECONDS)


def _group_is_empty(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


_STABLE_ENVIRONMENT = {"TZ": "UTC0", "LC_ALL": "C"}


def probe_process(pid: int, *, inherited_environment: bool = False) -> ProcessIdentity | None:
    """Identify the process holding ``pid``, or None if there is none.

    On macOS the start time comes from ``ps`` and is formatted by the caller's time zone and
    locale, so new records probe under a fixed environment and compare equal from any
    process. ``inherited_environment`` reproduces the old probe for a record written before
    that, which has to be compared the way it was recorded.
    """
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
            env=None if inherited_environment else {**os.environ, **_STABLE_ENVIRONMENT},
        )
        start_identity = completed.stdout.strip()
    elif sys.platform.startswith("linux"):
        try:
            start_identity = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
        except (FileNotFoundError, IndexError) as error:
            if not _pid_exists(pid):
                return None
            raise InternalInvariantViolationError(
                "cannot establish Linux process-start identity"
            ) from error
    else:
        raise InvalidInputError("worker supervision supports only macOS and Linux")
    if not start_identity:
        # The process can exit between the existence check and the identity read.
        if not _pid_exists(pid):
            return None
        raise InternalInvariantViolationError("process-start identity was empty")
    return ProcessIdentity(pid, process_group_id, start_identity, probe_host())


def probe_like(recorded: ProcessIdentity) -> ProcessIdentity | None:
    """Probe a recorded worker's pid the way that record was made (legacy: as inherited)."""
    return probe_process(recorded.pid, inherited_environment=recorded.legacy)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


_BOOT_TIME = re.compile(r"sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)")
_PLATFORM_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')


def _command_output(command: list[str]) -> str:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False).stdout.strip()
    except OSError:
        return ""


def _first_file_text(*paths: str) -> str:
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return ""


@functools.cache
def probe_host() -> HostIdentity:
    """Identify this machine and boot from ids that are stable across processes.

    The machine id is the platform UUID (macOS ``IOPlatformUUID``, Linux ``/etc/machine-id``)
    and the boot id the kernel's per-boot identity (macOS ``kern.bootsessionuuid``, falling
    back to the numeric ``sec.usec`` of ``kern.boottime``, never its time-zone formatted text;
    Linux ``boot_id``). The hostname is recorded for display only and is never compared.
    An id that cannot be read fails closed rather than being guessed.
    """
    hostname = socket.gethostname().strip()
    if sys.platform == "darwin":
        found = _PLATFORM_UUID.search(
            _command_output(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        )
        machine_id = found.group(1) if found else ""
        boot_id = _command_output(["sysctl", "-n", "kern.bootsessionuuid"])
        if not boot_id:
            boot_time = _BOOT_TIME.search(_command_output(["sysctl", "-n", "kern.boottime"]))
            boot_id = f"{boot_time.group(1)}.{boot_time.group(2)}" if boot_time else ""
    elif sys.platform.startswith("linux"):
        machine_id = _first_file_text("/etc/machine-id", "/var/lib/dbus/machine-id")
        boot_id = _first_file_text("/proc/sys/kernel/random/boot_id")
    else:
        raise InvalidInputError("worker supervision supports only macOS and Linux")
    if not machine_id or not boot_id:
        raise InternalInvariantViolationError(
            "cannot establish this host's machine or boot identity; refusing to guess"
        )
    return HostIdentity(machine_id, boot_id, hostname)


class Locality(enum.Enum):
    """Where a recorded worker's pid is meaningful relative to this host and boot."""

    LOCAL = "local"
    REBOOTED = "rebooted"
    FOREIGN = "foreign"


def classify_locality(identity: ProcessIdentity) -> Locality:
    """Place a recorded identity: legacy records assume this host.

    The same machine id with a different boot id means the host rebooted, so the worker
    cannot have survived; a different machine id says nothing about this host's processes.
    """
    if identity.host is None:
        return Locality.LOCAL
    here = probe_host()
    if identity.host.machine_id != here.machine_id:
        return Locality.FOREIGN
    if identity.host.boot_id != here.boot_id:
        return Locality.REBOOTED
    return Locality.LOCAL


def is_local(identity: ProcessIdentity) -> bool:
    """Whether a recorded process can be probed here: legacy records assume this host."""
    return classify_locality(identity) is Locality.LOCAL


def require_local(identity: ProcessIdentity, *, action: str) -> None:
    """Refuse to probe, signal or reap a pid recorded on another host or boot.

    Such a process is neither provably alive nor provably dead from here: the same pid on
    this machine names an unrelated process. The operator must reconcile it.
    """
    if not is_local(identity):
        raise HumanActionRequiredError(
            f"worker was launched on a different host or boot; it cannot be {action}d locally",
            details={
                "pid": identity.pid,
                "recorded_machine_id": None if identity.host is None else identity.host.machine_id,
                "recorded_boot_id": None if identity.host is None else identity.host.boot_id,
                "recorded_hostname": None if identity.host is None else identity.host.hostname,
                "current_machine_id": probe_host().machine_id,
                "current_boot_id": probe_host().boot_id,
                "current_hostname": probe_host().hostname,
            },
        )


def same_process(recorded: ProcessIdentity, observed: ProcessIdentity) -> bool:
    """Compare a recorded process with the one now holding its pid on this host.

    Callers establish locality first with ``require_local``; the host is therefore not part
    of the comparison, which keeps legacy and host-bound records comparable.
    """
    return (
        recorded.pid == observed.pid
        and recorded.process_group_id == observed.process_group_id
        and recorded.start_identity == observed.start_identity
    )


def identity_from_state(state: Mapping[str, object]) -> ProcessIdentity:
    """Decode a persisted identity, accepting legacy records and failing on unknown versions."""
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
    version = state.get("identity_version")
    if version is None:
        return ProcessIdentity(pid, process_group_id, start_identity)
    machine_id = state.get("machine_id")
    boot_id = state.get("boot_id")
    hostname = state.get("hostname", "")
    if (
        version != IDENTITY_VERSION
        or not isinstance(machine_id, str)
        or not machine_id
        or not isinstance(boot_id, str)
        or not boot_id
        or not isinstance(hostname, str)
    ):
        raise InternalInvariantViolationError(
            "stored supervisor host identity is invalid or from an unsupported version",
            details={"identity_version": str(version)},
        )
    return ProcessIdentity(
        pid, process_group_id, start_identity, HostIdentity(machine_id, boot_id, hostname)
    )


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
        if identity.host is not None:
            state.update(
                {
                    "identity_version": IDENTITY_VERSION,
                    "machine_id": identity.host.machine_id,
                    "boot_id": identity.host.boot_id,
                    "hostname": identity.host.hostname,
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


__all__ = [
    "IDENTITY_VERSION",
    "HostIdentity",
    "Locality",
    "ProcessIdentity",
    "WorkerSupervisor",
    "classify_locality",
    "identity_from_state",
    "is_local",
    "probe_host",
    "probe_like",
    "probe_process",
    "require_local",
    "same_process",
]
