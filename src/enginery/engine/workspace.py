"""Run-scoped workspace reservations with coordinator fencing.

A reservation is either a ``git worktree`` of the operator's repository (trusted-local; a
worktree is not containment) or a self-contained task-local clone that shares nothing with
the operator's repository (the isolated profile).
"""

from __future__ import annotations

import subprocess
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
from enginery.engine.task_repository import (
    BUNDLE_FILE_NAME,
    intake_branch_bundle,
    intake_path_for,
    materialize_task_local_repository,
    out_path_for,
    remove_task_local_workspace,
    verify_intake_branch,
)
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

_MANAGER_NAME = "workspace-reservations"

WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKSPACE_MODE_TASK_LOCAL_CLONE = "task-local-clone"
WORKSPACE_MODES = frozenset({WORKSPACE_MODE_GIT_WORKTREE, WORKSPACE_MODE_TASK_LOCAL_CLONE})


@dataclass(frozen=True, slots=True)
class WorkspaceReservation:
    repository_id: str
    run_id: str
    repository_path: Path
    workspace_path: Path
    base_revision: str
    status: str
    state_version: int
    mode: str = WORKSPACE_MODE_GIT_WORKTREE


class GitWorktreeBackend:
    """Reserve one repository workspace per run; worktrees are not containment."""

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

    def reserve(
        self,
        *,
        repository_id: str,
        run_id: str,
        repository_path: Path,
        workspace_path: Path,
        base_revision: str,
        mode: str = WORKSPACE_MODE_GIT_WORKTREE,
        epoch: int,
        now: datetime,
    ) -> WorkspaceReservation:
        _require_text(repository_id, "repository_id")
        _require_text(run_id, "run_id")
        _require_text(base_revision, "base_revision")
        if mode not in WORKSPACE_MODES:
            raise InvalidInputError("workspace mode is not supported")
        existing = self._ledger.read_process_manager_state(
            process_manager_name=_MANAGER_NAME, state_key=repository_id
        )
        if existing is not None:
            prior_run = existing.state.get("run_id")
            prior_status = existing.state.get("status")
            if prior_status in {"reserved", "materialized", "retained"} and prior_run != run_id:
                raise ExternalConflictError(
                    "repository workspace is reserved by another run",
                    details={"repository_id": repository_id, "run_id": prior_run},
                )
            if prior_status in {"reserved", "materialized", "retained"}:
                return _reservation_from_state(existing.state, existing.state_version)
        state = _reservation_state(
            repository_id, run_id, repository_path, workspace_path, base_revision, "reserved", mode
        )
        result = self._ledger.append(
            AppendCommand(
                correlation_id=f"workspace-reserve:{repository_id}:{run_id}",
                events=(
                    _workspace_event(self._ledger, repository_id, "workspace.reserved", state),
                ),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=epoch, now=now),
                    ProcessManagerStateWrite(
                        process_manager_name=_MANAGER_NAME,
                        state_key=repository_id,
                        expected_version=0 if existing is None else existing.state_version,
                        state=state,
                    ),
                ),
            )
        )
        reservation = WorkspaceReservation(
            repository_id,
            run_id,
            repository_path,
            workspace_path,
            base_revision,
            "reserved",
            result.process_manager_states[1].state_version,
            mode,
        )
        self._fault("workspace_reserved")
        return reservation

    def materialize(
        self, reservation: WorkspaceReservation, *, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        if reservation.status != "reserved":
            raise InvalidInputError("only a reserved workspace can be materialized")
        if reservation.mode == WORKSPACE_MODE_TASK_LOCAL_CLONE:
            materialize_task_local_repository(
                repository_path=reservation.repository_path,
                workspace_path=reservation.workspace_path,
                base_revision=reservation.base_revision,
            )
            materialized = self._record_status(
                reservation, status="materialized", epoch=epoch, now=now
            )
            self._fault("workspace_materialized")
            return materialized
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(reservation.repository_path),
                "worktree",
                "add",
                "--detach",
                str(reservation.workspace_path),
                reservation.base_revision,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ExternalConflictError(
                "git worktree creation failed",
                details={
                    "stderr": completed.stderr.strip(),
                    "repository_id": reservation.repository_id,
                },
            )
        materialized = self._record_status(reservation, status="materialized", epoch=epoch, now=now)
        self._fault("workspace_materialized")
        return materialized

    def verify_implementation_branch(
        self, reservation: WorkspaceReservation, *, head_branch: str
    ) -> str:
        """Verify one implemented, pushed branch remains bound to this workspace base."""
        if reservation.status not in {"materialized", "retained"}:
            raise InvalidInputError("only a materialized workspace can verify its branch")
        _require_text(head_branch, "head_branch")
        if reservation.mode == WORKSPACE_MODE_TASK_LOCAL_CLONE:
            return self._verify_task_local_branch(reservation, head_branch=head_branch)
        branch = self._run_git(
            reservation.workspace_path, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if branch != head_branch:
            raise ExternalConflictError(
                "workspace branch differs from the requested pull-request branch",
                details={"expected_branch": head_branch, "actual_branch": branch},
            )
        head_revision = self._run_git(reservation.workspace_path, "rev-parse", "HEAD")
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(reservation.workspace_path),
                "merge-base",
                "--is-ancestor",
                reservation.base_revision,
                head_revision,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if ancestor.returncode != 0:
            raise ExternalConflictError(
                "workspace branch does not descend from its reserved base revision",
                details={
                    "base_revision": reservation.base_revision,
                    "head_revision": head_revision,
                },
            )
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(reservation.workspace_path),
                "diff",
                "--quiet",
                f"{reservation.base_revision}..{head_revision}",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if changed.returncode == 0:
            raise ExternalConflictError(
                "workspace branch contains no implementation change",
                details={"head_revision": head_revision},
            )
        if changed.returncode != 1:
            raise ExternalConflictError(
                "workspace change verification failed",
                details={"stderr": changed.stderr.strip()},
            )
        remote = self._run_git(
            reservation.workspace_path,
            "ls-remote",
            "--exit-code",
            "origin",
            f"refs/heads/{head_branch}",
        )
        remote_revision = remote.partition("\t")[0]
        if remote_revision != head_revision:
            raise ExternalConflictError(
                "origin branch differs from the verified workspace revision",
                details={
                    "expected_head_revision": head_revision,
                    "remote_head_revision": remote_revision,
                },
            )
        return head_revision

    def _verify_task_local_branch(
        self, reservation: WorkspaceReservation, *, head_branch: str
    ) -> str:
        """Verify the worker's bundled branch from a host-owned intake repository.

        The worker's own repository (its config and hooks) is worker-controlled, so the host
        never runs git inside it: it imports only the bundle the worker wrote, into an intake
        repository the host created, and verifies the branch there. The branch is not on
        any remote at this point; publishing it is a separate, host-side effect.
        """
        intake_branch_bundle(
            repository_path=reservation.repository_path,
            workspace_path=reservation.workspace_path,
            bundle_path=out_path_for(reservation.workspace_path) / BUNDLE_FILE_NAME,
            head_branch=head_branch,
            base_revision=reservation.base_revision,
        )
        return verify_intake_branch(
            intake_path=intake_path_for(reservation.workspace_path),
            head_branch=head_branch,
            base_revision=reservation.base_revision,
        )

    def cleanup(
        self, reservation: WorkspaceReservation, *, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        if reservation.status not in {"materialized", "reserved", "retained", "cleanup_started"}:
            raise InvalidInputError("workspace is already cleaned")
        # A cleanup interrupted after it was recorded as started resumes where it stopped.
        cleaning = (
            reservation
            if reservation.status == "cleanup_started"
            else self._record_status(reservation, status="cleanup_started", epoch=epoch, now=now)
        )
        self._fault("workspace_cleanup_started")
        if cleaning.mode == WORKSPACE_MODE_TASK_LOCAL_CLONE:
            leftover = remove_task_local_workspace(workspace_path=cleaning.workspace_path)
            if leftover:
                retained = self._record_status(cleaning, status="retained", epoch=epoch, now=now)
                self._fault("workspace_cleanup_recorded")
                raise ExternalConflictError(
                    "task-local workspace cleanup left files behind; workspace retained for "
                    "reconciliation",
                    details={
                        "workspace_path": str(retained.workspace_path),
                        "retained_paths": ", ".join(str(path) for path in leftover),
                    },
                )
            cleaned = self._record_status(cleaning, status="cleaned", epoch=epoch, now=now)
            self._fault("workspace_cleanup_recorded")
            return cleaned
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(cleaning.repository_path),
                "worktree",
                "remove",
                "--force",
                str(cleaning.workspace_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            retained = self._record_status(cleaning, status="retained", epoch=epoch, now=now)
            self._fault("workspace_cleanup_recorded")
            raise ExternalConflictError(
                "git worktree cleanup failed; workspace retained for reconciliation",
                details={
                    "workspace_path": str(retained.workspace_path),
                    "stderr": completed.stderr.strip(),
                },
            )
        cleaned = self._record_status(cleaning, status="cleaned", epoch=epoch, now=now)
        self._fault("workspace_cleanup_recorded")
        return cleaned

    def _record_status(
        self, reservation: WorkspaceReservation, *, status: str, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        state = _reservation_state(
            reservation.repository_id,
            reservation.run_id,
            reservation.repository_path,
            reservation.workspace_path,
            reservation.base_revision,
            status,
            reservation.mode,
        )
        result = self._ledger.append(
            AppendCommand(
                correlation_id=f"workspace-{status}:{reservation.repository_id}:{reservation.run_id}",
                events=(
                    _workspace_event(
                        self._ledger, reservation.repository_id, f"workspace.{status}", state
                    ),
                ),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=epoch, now=now),
                    ProcessManagerStateWrite(
                        process_manager_name=_MANAGER_NAME,
                        state_key=reservation.repository_id,
                        expected_version=reservation.state_version,
                        state=state,
                    ),
                ),
            )
        )
        return WorkspaceReservation(
            reservation.repository_id,
            reservation.run_id,
            reservation.repository_path,
            reservation.workspace_path,
            reservation.base_revision,
            status,
            result.process_manager_states[1].state_version,
            reservation.mode,
        )

    def retain(
        self, reservation: WorkspaceReservation, *, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        """Retain a materialized workspace while an operator decision is pending."""
        if reservation.status not in {"materialized", "cleanup_started", "retained"}:
            raise InvalidInputError("only an active workspace can be retained")
        return self._record_status(reservation, status="retained", epoch=epoch, now=now)

    def resume(
        self, reservation: WorkspaceReservation, *, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        """Return a retained, present workspace to the scheduler."""
        if reservation.status != "retained":
            raise InvalidInputError("only a retained workspace can be resumed")
        if not reservation.workspace_path.is_dir():
            raise ExternalConflictError(
                "retained workspace is missing; human reconciliation is required",
                details={"workspace_path": str(reservation.workspace_path)},
            )
        return self._record_status(reservation, status="materialized", epoch=epoch, now=now)

    def read_reservation(self, repository_id: str) -> WorkspaceReservation | None:
        record = self._ledger.read_process_manager_state(
            process_manager_name=_MANAGER_NAME, state_key=repository_id
        )
        if record is None:
            return None
        return _reservation_from_state(record.state, record.state_version)

    def list_reservations(self) -> tuple[WorkspaceReservation, ...]:
        """Return every repository's current reservation, from the same durable
        state :meth:`read_reservation` reads -- no separate listing store."""
        records = self._ledger.list_process_manager_states(process_manager_name=_MANAGER_NAME)
        return tuple(
            _reservation_from_state(record.state, record.state_version) for record in records
        )

    @staticmethod
    def _run_git(path: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(path), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ExternalConflictError(
                "git branch verification failed",
                details={"arguments": arguments, "stderr": completed.stderr.strip()},
            )
        return completed.stdout.strip()

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)


def _workspace_event(
    ledger: LedgerService, repository_id: str, event_type: str, payload: dict[str, object]
) -> EventWrite:
    projection = ledger.read_projection(aggregate_type="workspace", aggregate_id=repository_id)
    return EventWrite(
        "workspace",
        repository_id,
        0 if projection is None else projection.aggregate_version,
        event_type,
        1,
        payload,
    )


def _reservation_state(
    repository_id: str,
    run_id: str,
    repository_path: Path,
    workspace_path: Path,
    base_revision: str,
    status: str,
    mode: str = WORKSPACE_MODE_GIT_WORKTREE,
) -> dict[str, object]:
    state: dict[str, object] = {
        "repository_id": repository_id,
        "run_id": run_id,
        "repository_path": str(repository_path),
        "workspace_path": str(workspace_path),
        "base_revision": base_revision,
        "status": status,
    }
    if mode != WORKSPACE_MODE_GIT_WORKTREE:
        # A reservation persisted before workspace modes existed has no key and decodes as
        # a git worktree, so its stored state is exactly what it was.
        state["mode"] = mode
    return state


def _reservation_from_state(state: object, state_version: int) -> WorkspaceReservation:
    if not isinstance(state, dict):
        raise InternalInvariantViolationError("workspace reservation state is invalid")
    repository_id = state.get("repository_id")
    run_id = state.get("run_id")
    repository_path = state.get("repository_path")
    workspace_path = state.get("workspace_path")
    base_revision = state.get("base_revision")
    status = state.get("status")
    values = (repository_id, run_id, repository_path, workspace_path, base_revision, status)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise InternalInvariantViolationError("workspace reservation state is incomplete")
    assert isinstance(repository_id, str)
    assert isinstance(run_id, str)
    assert isinstance(repository_path, str)
    assert isinstance(workspace_path, str)
    assert isinstance(base_revision, str)
    assert isinstance(status, str)
    mode = state.get("mode", WORKSPACE_MODE_GIT_WORKTREE)
    if not isinstance(mode, str) or mode not in WORKSPACE_MODES:
        raise InternalInvariantViolationError("workspace reservation mode is invalid")
    return WorkspaceReservation(
        repository_id,
        run_id,
        Path(repository_path),
        Path(workspace_path),
        base_revision,
        status,
        state_version,
        mode,
    )


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise InvalidInputError(f"{field_name} must be non-blank")


__all__ = [
    "WORKSPACE_MODES",
    "WORKSPACE_MODE_GIT_WORKTREE",
    "WORKSPACE_MODE_TASK_LOCAL_CLONE",
    "GitWorktreeBackend",
    "WorkspaceReservation",
]
