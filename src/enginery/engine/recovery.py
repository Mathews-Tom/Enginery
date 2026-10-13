"""Fail-closed worker and workspace recovery checks."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from enginery.domain.errors import HumanActionRequiredError, InternalInvariantViolationError
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
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateRecord, ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

_SUPERVISOR_NAME = "worker-supervisor"


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

    def __init__(self, ledger: LedgerService, coordinator: Coordinator) -> None:
        self._ledger = ledger
        self._coordinator = coordinator
        self._leases = FencedNodeLeases(ledger, coordinator)

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
        assessment = assess_orphan(process_state=process_state, workspace_path=workspace_path)
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
        self, *, lease: FencedNodeLease, workspace_path: Path, epoch: int, now: datetime
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
        assessment = assess_orphan(process_state=record, workspace_path=workspace_path)
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
    *, process_state: ProcessManagerStateRecord, workspace_path: Path
) -> RecoveryAssessment:
    """Prove a prior worker is absent and its workspace is quiescent.

    A worker is absent when its pid is free, held by a different process, or its host has
    rebooted since it launched. A malformed or foreign-machine record, a live process, a Git
    failure or an in-progress Git lock blocks automatic recovery.
    """
    liveness = assess_worker(process_state.state).liveness
    if liveness is WorkerLiveness.UNVERIFIABLE:
        return RecoveryAssessment(False, "supervisor_identity_missing_or_invalid")
    if liveness is WorkerLiveness.FOREIGN_HOST:
        return RecoveryAssessment(False, "worker_host_identity_differs")
    if liveness is WorkerLiveness.ALIVE:
        return RecoveryAssessment(False, "process_still_running")
    return assess_workspace_quiescence(workspace_path)


def assess_workspace_quiescence(workspace_path: Path) -> RecoveryAssessment:
    if not workspace_path.is_dir():
        return RecoveryAssessment(False, "workspace_missing")
    lock = subprocess.run(
        ["git", "-C", str(workspace_path), "rev-parse", "--git-path", "index.lock"],
        text=True,
        capture_output=True,
        check=False,
    )
    if lock.returncode != 0:
        return RecoveryAssessment(False, "workspace_identity_unreadable")
    lock_path = Path(lock.stdout.strip())
    if not lock_path.is_absolute():
        lock_path = workspace_path / lock_path
    if lock_path.exists():
        return RecoveryAssessment(False, "workspace_git_lock_present")
    status = subprocess.run(
        ["git", "-C", str(workspace_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if status.returncode != 0:
        return RecoveryAssessment(False, "workspace_inspection_failed")
    return RecoveryAssessment(True, "process_absent_workspace_quiescent")


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
