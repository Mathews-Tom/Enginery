"""Run-scoped Git worktree reservations with coordinator fencing."""

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
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

_MANAGER_NAME = "workspace-reservations"


@dataclass(frozen=True, slots=True)
class WorkspaceReservation:
    repository_id: str
    run_id: str
    repository_path: Path
    workspace_path: Path
    base_revision: str
    status: str
    state_version: int


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
        epoch: int,
        now: datetime,
    ) -> WorkspaceReservation:
        _require_text(repository_id, "repository_id")
        _require_text(run_id, "run_id")
        _require_text(base_revision, "base_revision")
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
            repository_id, run_id, repository_path, workspace_path, base_revision, "reserved"
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
        )
        self._fault("workspace_reserved")
        return reservation

    def materialize(
        self, reservation: WorkspaceReservation, *, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        if reservation.status != "reserved":
            raise InvalidInputError("only a reserved workspace can be materialized")
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

    def cleanup(
        self, reservation: WorkspaceReservation, *, epoch: int, now: datetime
    ) -> WorkspaceReservation:
        if reservation.status not in {"materialized", "reserved", "retained"}:
            raise InvalidInputError("workspace is already cleaned")
        cleaning = self._record_status(reservation, status="cleanup_started", epoch=epoch, now=now)
        self._fault("workspace_cleanup_started")
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
) -> dict[str, object]:
    return {
        "repository_id": repository_id,
        "run_id": run_id,
        "repository_path": str(repository_path),
        "workspace_path": str(workspace_path),
        "base_revision": base_revision,
        "status": status,
    }


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
    return WorkspaceReservation(
        repository_id,
        run_id,
        Path(repository_path),
        Path(workspace_path),
        base_revision,
        status,
        state_version,
    )


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise InvalidInputError(f"{field_name} must be non-blank")


__all__ = ["GitWorktreeBackend", "WorkspaceReservation"]
