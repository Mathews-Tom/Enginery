"""Coordinator-owned fixture dispatch with durable lease and workspace boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enginery.domain.errors import InvalidInputError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.supervisor import ProcessIdentity, WorkerSupervisor
from enginery.engine.workspace import GitWorktreeBackend, WorkspaceReservation
from enginery.ledger.service import LedgerService


@dataclass(frozen=True, slots=True)
class FixtureDispatch:
    """One prevalidated fixture-node execution request."""

    run_id: str
    node_id: str
    attempt_id: str
    repository_id: str
    repository_path: Path
    workspace_path: Path
    base_revision: str
    command: tuple[str, ...]
    expected_attempt_version: int

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.run_id,
                self.node_id,
                self.attempt_id,
                self.repository_id,
                self.base_revision,
            )
        ):
            raise InvalidInputError("fixture dispatch identifiers must be non-blank")
        if not self.command:
            raise InvalidInputError("fixture dispatch command must not be empty")
        if self.expected_attempt_version < 0:
            raise InvalidInputError("expected_attempt_version cannot be negative")


@dataclass(frozen=True, slots=True)
class DispatchedFixture:
    lease: FencedNodeLease
    identity: ProcessIdentity
    workspace: WorkspaceReservation


class FixtureRuntime:
    """Drive one fixture attempt through reservation, lease, and supervision.

    Each durable boundary precedes the corresponding side effect. A crash
    leaves a reservation or lease to be reconciled rather than allowing a
    second worker to start.
    """

    def __init__(
        self,
        ledger: LedgerService,
        coordinator: Coordinator,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._fault_hook = fault_hook
        self._leases = FencedNodeLeases(ledger, coordinator)
        self._supervisor = WorkerSupervisor(ledger, coordinator)
        self._workspaces = GitWorktreeBackend(ledger, coordinator)

    def dispatch(
        self,
        *,
        request: FixtureDispatch,
        epoch: int,
        now: datetime,
        lease_window: timedelta,
    ) -> DispatchedFixture:
        reservation = self._workspaces.reserve(
            repository_id=request.repository_id,
            run_id=request.run_id,
            repository_path=request.repository_path,
            workspace_path=request.workspace_path,
            base_revision=request.base_revision,
            epoch=epoch,
            now=now,
        )
        self._fault("workspace_reserved")
        if reservation.status == "reserved":
            reservation = self._workspaces.materialize(reservation, epoch=epoch, now=now)
        self._fault("workspace_materialized")
        if reservation.status != "materialized":
            raise InvalidInputError("fixture dispatch requires a materialized workspace")
        lease = self._leases.grant(
            run_id=request.run_id,
            node_id=request.node_id,
            attempt_id=request.attempt_id,
            epoch=epoch,
            now=now,
            lease_window=lease_window,
            expected_attempt_version=request.expected_attempt_version,
        )
        self._fault("lease_granted")
        identity = self._supervisor.start(
            lease=lease,
            command=request.command,
            cwd=reservation.workspace_path,
            now=now,
        )
        self._fault("worker_started")
        return DispatchedFixture(lease=lease, identity=identity, workspace=reservation)

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)


__all__ = ["DispatchedFixture", "FixtureDispatch", "FixtureRuntime"]
