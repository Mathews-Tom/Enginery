"""Fail-closed worker and workspace recovery checks."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enginery.domain.errors import HumanActionRequiredError, InternalInvariantViolationError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.supervisor import ProcessIdentity, probe_process
from enginery.ledger.process_manager import ProcessManagerStateRecord
from enginery.ledger.service import LedgerService


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    ready_to_release: bool
    reason: str


class RecoveryCoordinator:
    """Re-lease only after durable orphan and workspace proof succeeds."""

    def __init__(self, ledger: LedgerService, coordinator: Coordinator) -> None:
        self._ledger = ledger
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
        workspace_path: Path,
    ) -> FencedNodeLease:
        process_state = self._ledger.read_process_manager_state(
            process_manager_name="worker-supervisor", state_key=f"{run_id}:{node_id}"
        )
        if process_state is None:
            raise HumanActionRequiredError("missing prior worker supervision evidence")
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
        )


def assess_orphan(
    *, process_state: ProcessManagerStateRecord, workspace_path: Path
) -> RecoveryAssessment:
    """Prove a prior worker is absent and its workspace is quiescent.

    Any malformed process state, PID reuse, live process, Git failure, or
    in-progress Git lock blocks automatic recovery.
    """
    try:
        identity = _identity_from_state(process_state)
    except InternalInvariantViolationError:
        return RecoveryAssessment(False, "supervisor_identity_missing_or_invalid")
    observed = probe_process(identity.pid)
    if observed is not None:
        if observed != identity:
            return RecoveryAssessment(False, "process_identity_changed")
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


def _identity_from_state(record: ProcessManagerStateRecord) -> ProcessIdentity:
    state = record.state
    pid = state.get("pid")
    process_group_id = state.get("process_group_id")
    start_identity = state.get("start_identity")
    if (
        not isinstance(pid, int)
        or not isinstance(process_group_id, int)
        or not isinstance(start_identity, str)
    ):
        raise InternalInvariantViolationError("stored supervisor process identity is invalid")
    return ProcessIdentity(pid, process_group_id, start_identity)


__all__ = [
    "RecoveryAssessment",
    "RecoveryCoordinator",
    "assess_orphan",
    "assess_workspace_quiescence",
]
