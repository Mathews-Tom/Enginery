"""Fail-closed worker and workspace recovery checks."""

from __future__ import annotations

import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from enginery.domain.errors import (
    ExternalConflictError,
    HumanActionRequiredError,
    InternalInvariantViolationError,
)
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.supervisor import (
    Locality,
    Presence,
    ProcessIdentity,
    classify_locality,
    identity_from_state,
    probe_presence,
)
from enginery.engine.task_repository import run_worktree_git
from enginery.engine.workspace import (
    WORKSPACE_MODE_GIT_WORKTREE,
    WORKSPACE_MODE_TASK_LOCAL_CLONE,
    GitWorktreeBackend,
)
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateRecord, ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

_SUPERVISOR_NAME = "worker-supervisor"
_GIT_TIMEOUT_SECONDS = 5.0


class WorkerLiveness(StrEnum):
    """What this host can prove about a worker from its persisted identity alone."""

    ALIVE = "alive"
    EXITED = "exited"
    PID_REUSED = "pid_reused"
    REBOOTED = "rebooted"
    FOREIGN_HOST = "foreign_host"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class WorkerAssessment:
    liveness: WorkerLiveness
    identity: ProcessIdentity | None


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    ready_to_release: bool
    reason: str


class RecoveryCoordinator:
    """Re-lease only after durable orphan and workspace proof succeeds."""

    def __init__(
        self,
        ledger: LedgerService,
        coordinator: Coordinator,
        *,
        container_gone: Callable[[str], bool] | None = None,
    ) -> None:
        self._ledger = ledger
        self._coordinator = coordinator
        self._leases = FencedNodeLeases(ledger, coordinator)
        self._container_gone = container_gone

    def _mode_for(self, workspace_path: Path, explicit: str | None) -> str:
        """The workspace mode recorded for ``workspace_path``; explicit wins, else the reservation.

        A path that any durable reservation records as a task-local clone is one, so recovery
        never runs git there. A path no reservation names was not created by the engine's
        clone path and is handled as a linked worktree.
        """
        if explicit is not None:
            return explicit
        modes = {
            reservation.mode
            for reservation in GitWorktreeBackend(
                self._ledger, self._coordinator
            ).list_reservations()
            if reservation.workspace_path == workspace_path
        }
        if WORKSPACE_MODE_TASK_LOCAL_CLONE in modes:
            return WORKSPACE_MODE_TASK_LOCAL_CLONE
        return WORKSPACE_MODE_GIT_WORKTREE

    def re_lease(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt_id: str,
        epoch: int,
        now: datetime,
        lease_window: timedelta,
        expected_attempt_version: int,
        operation_id: str,
        workspace_path: Path,
        workspace_mode: str | None = None,
    ) -> FencedNodeLease:
        process_state = self._ledger.read_process_manager_state(
            process_manager_name="worker-supervisor", state_key=f"{run_id}:{node_id}"
        )
        if process_state is None:
            raise HumanActionRequiredError("missing prior worker supervision evidence")
        if process_state.state.get("status") != "exit_observed":
            raise HumanActionRequiredError(
                "prior worker must be reconciled before replacement lease issuance"
            )
        assessment = assess_orphan(
            process_state=process_state,
            workspace_path=workspace_path,
            workspace_mode=self._mode_for(workspace_path, workspace_mode),
            container_gone=self._container_gone,
        )
        if not assessment.ready_to_release:
            raise HumanActionRequiredError(
                "automatic recovery blocked pending human reconciliation",
                details={"reason": assessment.reason},
            )
        return self._leases.grant(
            run_id=run_id,
            node_id=node_id,
            attempt_id=attempt_id,
            epoch=epoch,
            now=now,
            lease_window=lease_window,
            expected_attempt_version=expected_attempt_version,
            operation_id=operation_id,
        )

    def reconcile(
        self,
        *,
        lease: FencedNodeLease,
        workspace_path: Path,
        epoch: int,
        now: datetime,
        workspace_mode: str | None = None,
    ) -> RecoveryAssessment:
        """Record observed prior-worker absence under the replacement epoch."""
        record = self._ledger.read_process_manager_state(
            process_manager_name=_SUPERVISOR_NAME, state_key=f"{lease.run_id}:{lease.node_id}"
        )
        if record is None:
            return RecoveryAssessment(False, "supervisor_state_missing")
        if record.state.get("attempt_id") != lease.attempt_id:
            return RecoveryAssessment(False, "worker_attempt_identity_mismatch")
        if record.state.get("status") == "exit_reconciled":
            return RecoveryAssessment(True, "already_reconciled")
        assessment = assess_orphan(
            process_state=record,
            workspace_path=workspace_path,
            workspace_mode=self._mode_for(workspace_path, workspace_mode),
            container_gone=self._container_gone,
        )
        if not assessment.ready_to_release:
            return assessment
        state = dict(record.state)
        state["status"] = "exit_reconciled"
        state["reconciled_epoch"] = epoch
        projection = self._ledger.read_projection(
            aggregate_type="worker", aggregate_id=f"{lease.run_id}:{lease.node_id}"
        )
        self._ledger.append(
            AppendCommand(
                correlation_id=(
                    f"worker-reconciled:{lease.run_id}:{lease.node_id}:{lease.fencing_token}"
                ),
                events=(
                    EventWrite(
                        aggregate_type="worker",
                        aggregate_id=f"{lease.run_id}:{lease.node_id}",
                        expected_version=0 if projection is None else projection.aggregate_version,
                        event_type="worker.exit_reconciled",
                        schema_version=1,
                        payload=state,
                    ),
                ),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=epoch, now=now),
                    ProcessManagerStateWrite(
                        _SUPERVISOR_NAME, record.state_key, record.state_version, state
                    ),
                ),
            )
        )
        return assessment


def assess_worker(state: Mapping[str, object]) -> WorkerAssessment:
    """Classify a persisted worker by its start, process-group and host identity.

    A worker that is gone (death) and a worker that is still running while its coordinator
    lapsed (heartbeat loss) are different facts: only a process whose recorded start identity
    still holds its pid on this host and boot is ``ALIVE``. ``EXITED``, ``PID_REUSED`` (same
    machine and boot, the pid now belongs to another process) and ``REBOOTED`` (same machine,
    different boot) all prove the recorded worker is gone. A record from a different machine,
    or one that cannot be read, proves nothing and is never classified by a local probe.
    """
    try:
        identity = identity_from_state(state)
    except InternalInvariantViolationError:
        return WorkerAssessment(WorkerLiveness.UNVERIFIABLE, None)
    try:
        locality = classify_locality(identity)
    except InternalInvariantViolationError:
        # This host's own identity cannot be established, so nothing can be proven.
        return WorkerAssessment(WorkerLiveness.UNVERIFIABLE, identity)
    if locality is Locality.FOREIGN:
        return WorkerAssessment(WorkerLiveness.FOREIGN_HOST, identity)
    if locality is Locality.REBOOTED:
        return WorkerAssessment(WorkerLiveness.REBOOTED, identity)
    presence = probe_presence(identity)
    if presence is Presence.UNPROVABLE:
        # A legacy record's start time was formatted in an unknown time zone, so a mismatch
        # may still be the same live process: fail closed.
        return WorkerAssessment(WorkerLiveness.UNVERIFIABLE, identity)
    if presence is Presence.ABSENT:
        return WorkerAssessment(WorkerLiveness.EXITED, identity)
    if presence is Presence.OTHER:
        # A pid this user cannot signal, or one with another start identity, is not our worker.
        return WorkerAssessment(WorkerLiveness.PID_REUSED, identity)
    return WorkerAssessment(WorkerLiveness.ALIVE, identity)


GONE = frozenset({WorkerLiveness.EXITED, WorkerLiveness.PID_REUSED, WorkerLiveness.REBOOTED})
"""Liveness values that prove the recorded worker is no longer running."""


def assess_orphan(
    *,
    process_state: ProcessManagerStateRecord,
    workspace_path: Path,
    workspace_mode: str = WORKSPACE_MODE_GIT_WORKTREE,
    container_gone: Callable[[str], bool] | None = None,
) -> RecoveryAssessment:
    """Prove a prior worker is absent and its workspace is quiescent.

    A worker is absent when its pid is free, held by a different process, or its host has
    rebooted since it launched. A malformed or foreign-machine record, a live process, a Git
    failure or an in-progress Git lock blocks automatic recovery. For a task-local clone the
    supervised process is the launcher that owns the runner container, so its absence plus
    (when ``container_gone`` is supplied) the absence of that operation's container is the
    proof; no git runs inside the worker-writable workspace.
    """
    liveness = assess_worker(process_state.state).liveness
    if liveness is WorkerLiveness.UNVERIFIABLE:
        return RecoveryAssessment(False, "supervisor_identity_missing_or_invalid")
    if liveness is WorkerLiveness.FOREIGN_HOST:
        return RecoveryAssessment(False, "worker_host_identity_differs")
    if liveness is WorkerLiveness.ALIVE:
        return RecoveryAssessment(False, "process_still_running")
    if workspace_mode == WORKSPACE_MODE_TASK_LOCAL_CLONE and container_gone is not None:
        operation_id = process_state.state.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            return RecoveryAssessment(False, "runner_container_unverifiable")
        try:
            gone = container_gone(operation_id)
        except Exception:
            return RecoveryAssessment(False, "runner_container_unverifiable")
        if not gone:
            return RecoveryAssessment(False, "runner_container_present")
    return assess_workspace_quiescence(workspace_path, workspace_mode=workspace_mode)


def assess_workspace_quiescence(
    workspace_path: Path, *, workspace_mode: str = WORKSPACE_MODE_GIT_WORKTREE
) -> RecoveryAssessment:
    if workspace_mode == WORKSPACE_MODE_TASK_LOCAL_CLONE:
        return _assess_task_local_quiescence(workspace_path)
    if workspace_mode != WORKSPACE_MODE_GIT_WORKTREE:
        return RecoveryAssessment(False, "workspace_mode_unknown")
    if not workspace_path.is_dir():
        return RecoveryAssessment(False, "workspace_missing")
    try:
        lock = run_worktree_git(
            ["rev-parse", "--git-path", "index.lock"],
            cwd=workspace_path,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except ExternalConflictError:
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    if lock.returncode != 0:
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    lock_path = Path(lock.stdout.strip())
    if not lock_path.is_absolute():
        lock_path = workspace_path / lock_path
    if lock_path.exists():
        return RecoveryAssessment(False, "workspace_git_lock_present")
    try:
        status = run_worktree_git(
            ["status", "--porcelain"], cwd=workspace_path, timeout=_GIT_TIMEOUT_SECONDS
        )
    except ExternalConflictError:
        return RecoveryAssessment(False, "workspace_inspection_failed")
    if status.returncode != 0:
        return RecoveryAssessment(False, "workspace_inspection_failed")
    return RecoveryAssessment(True, "process_absent_workspace_quiescent")


def _assess_task_local_quiescence(workspace_path: Path) -> RecoveryAssessment:
    """Quiescence of a worker-writable clone, proven without running any git in it.

    The worker controls the clone's configuration, so even ``git status`` could execute its
    ``core.fsmonitor``. Only non-following ``lstat`` calls are made: the workspace and its
    ``.git`` must be real directories and ``.git/index.lock`` must be absent.
    """
    try:
        workspace_info = workspace_path.lstat()
    except FileNotFoundError:
        return RecoveryAssessment(False, "workspace_missing")
    except OSError:
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    if not stat.S_ISDIR(workspace_info.st_mode):
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    git_dir = workspace_path / ".git"
    try:
        git_info = git_dir.lstat()
    except OSError:
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    if not stat.S_ISDIR(git_info.st_mode):
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    try:
        (git_dir / "index.lock").lstat()
    except FileNotFoundError:
        return RecoveryAssessment(True, "process_absent_workspace_quiescent")
    except OSError:
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    return RecoveryAssessment(False, "workspace_git_lock_present")


__all__ = [
    "GONE",
    "RecoveryAssessment",
    "RecoveryCoordinator",
    "WorkerAssessment",
    "WorkerLiveness",
    "assess_orphan",
    "assess_worker",
    "assess_workspace_quiescence",
]
