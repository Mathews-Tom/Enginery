"""Fail-closed worker and workspace recovery checks."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from enginery.domain.errors import InternalInvariantViolationError
from enginery.engine.supervisor import ProcessIdentity, probe_process
from enginery.ledger.process_manager import ProcessManagerStateRecord


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    ready_to_release: bool
    reason: str


def assess_orphan(
    *, process_state: ProcessManagerStateRecord, workspace_path: Path
) -> RecoveryAssessment:
    """Prove a prior worker is absent and its workspace is quiescent.

    Any malformed process state, PID reuse, live process, Git failure, or
    in-progress Git lock blocks automatic recovery.
    """
    identity = _identity_from_state(process_state)
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


__all__ = ["RecoveryAssessment", "assess_orphan", "assess_workspace_quiescence"]
