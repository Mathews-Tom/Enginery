"""Coordinator-owned runtime scheduling and worker-result ingestion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.engine.coordinator import CommandConsumption, Coordinator, CoordinatorEpoch
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.recovery import RecoveryCoordinator
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.scheduler import (
    NodeKey,
    ReadinessScheduler,
    SchedulableNode,
    SchedulableState,
    SchedulingLimits,
)
from enginery.engine.supervisor import ProcessIdentity, WorkerSupervisor
from enginery.engine.workspace import GitWorktreeBackend, WorkspaceReservation
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService

_RUNTIME_NODE = "runtime_node"


@dataclass(frozen=True, slots=True)
class FixtureDispatch:
    """One durable, provider-neutral fixture-node execution request."""

    run_id: str
    node_id: str
    attempt_id: str
    repository_id: str
    repository_path: Path
    workspace_path: Path
    base_revision: str
    command: tuple[str, ...]
    expected_attempt_version: int
    operation_id: str
    dependencies: tuple[tuple[str, str], ...] = ()
    workflow_definition_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.run_id,
                self.node_id,
                self.attempt_id,
                self.repository_id,
                self.base_revision,
                self.operation_id,
            )
        ):
            raise InvalidInputError("fixture dispatch identifiers must be non-blank")
        if not self.command:
            raise InvalidInputError("fixture dispatch command must not be empty")
        if self.expected_attempt_version < 0:
            raise InvalidInputError("expected_attempt_version cannot be negative")
        if any(not run_id.strip() or not node_id.strip() for run_id, node_id in self.dependencies):
            raise InvalidInputError("fixture dispatch dependencies must be non-blank node keys")
        if self.workflow_definition_id is not None and not self.workflow_definition_id.strip():
            raise InvalidInputError("workflow definition id must be non-blank when present")


@dataclass(frozen=True, slots=True)
class DispatchedFixture:
    lease: FencedNodeLease
    identity: ProcessIdentity
    workspace: WorkspaceReservation


@dataclass(frozen=True, slots=True)
class RuntimeTickResult:
    """Durable work completed by one coordinator-owned scheduling tick."""

    epoch: CoordinatorEpoch
    consumed_commands: tuple[CommandConsumption, ...]
    dispatched: tuple[DispatchedFixture, ...]


class FixtureRuntime:
    """Drive one fixture attempt through independent durable resources."""

    def __init__(
        self,
        ledger: LedgerService,
        coordinator: Coordinator,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._fault_hook = fault_hook
        self._leases = FencedNodeLeases(ledger, coordinator)
        self._supervisor = WorkerSupervisor(ledger, coordinator, fault_hook=fault_hook)
        self._workspaces = GitWorktreeBackend(ledger, coordinator, fault_hook=fault_hook)

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
        if reservation.status == "reserved":
            reservation = self._workspaces.materialize(reservation, epoch=epoch, now=now)
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
            operation_id=request.operation_id,
        )
        self._fault("lease_granted")
        identity = self._supervisor.start(
            lease=lease,
            command=request.command,
            cwd=reservation.workspace_path,
            now=now,
        )
        return DispatchedFixture(lease=lease, identity=identity, workspace=reservation)

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)


class CoordinatorRuntime:
    """The only runtime API that schedules, launches, and accepts results.

    It stores fixture requests as ``runtime_node`` projections. Replacement
    coordinators therefore derive readiness and active occupancy from the same
    ledger rather than from an in-memory queue. Workers only return typed
    envelopes; the coordinator appends every aggregate transition.
    """

    def __init__(
        self,
        ledger: LedgerService,
        *,
        owner: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._coordinator = Coordinator(ledger, owner=owner)
        self._fixture_runtime = FixtureRuntime(ledger, self._coordinator, fault_hook=fault_hook)
        self._leases = FencedNodeLeases(ledger, self._coordinator)
        self._workspaces = GitWorktreeBackend(ledger, self._coordinator, fault_hook=fault_hook)
        self._fault_hook = fault_hook
        self._last_run_id: str | None = None

    @property
    def coordinator(self) -> Coordinator:
        return self._coordinator

    def tick(
        self,
        *,
        now: datetime,
        heartbeat_window: timedelta,
        lease_window: timedelta,
        limits: SchedulingLimits,
        requests: tuple[FixtureDispatch, ...] = (),
    ) -> RuntimeTickResult:
        """Consume commands, derive durable readiness, and launch a fair batch."""
        epoch = self._acquire_or_renew(now=now, heartbeat_window=heartbeat_window)
        self._fault("epoch_acquired")
        consumed = self._coordinator.consume_pending(
            epoch=epoch.epoch,
            now=now,
            heartbeat_window=heartbeat_window,
        )
        for request in requests:
            self._register(request=request, epoch=epoch.epoch, now=now)
        requests_by_key = self._requests_from_ledger()
        nodes = tuple(
            _schedulable(request, state=_runtime_state(self._ledger, request))
            for request in requests_by_key.values()
        )
        plan = ReadinessScheduler().plan(
            nodes,
            limits=limits,
            last_run_id=self._last_run_id,
        )
        dispatched: list[DispatchedFixture] = []
        for key in plan.selected:
            request = requests_by_key[key]
            fixture = self._fixture_runtime.dispatch(
                request=request,
                epoch=epoch.epoch,
                now=now,
                lease_window=lease_window,
            )
            self._set_status(
                request=request,
                status="running",
                event_type="runtime_node.started",
                epoch=epoch.epoch,
                now=now,
            )
            dispatched.append(fixture)
        self._last_run_id = plan.next_run_id
        return RuntimeTickResult(epoch, consumed, tuple(dispatched))

    def ingest_result(self, *, envelope: WorkerResultEnvelope, now: datetime) -> None:
        """Validate and ingest an exact current-lease worker result, then clean up."""
        request = self._request_for(envelope.run_id, envelope.node_id)
        if request.attempt_id != envelope.attempt_id:
            raise ExternalConflictError("worker result attempt does not match durable runtime node")
        attempt = self._ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=envelope.attempt_id
        )
        if attempt is None:
            raise InternalInvariantViolationError("worker result has no durable node attempt")
        self._fault("result_received")
        WorkerSupervisor(self._ledger, self._coordinator, fault_hook=self._fault_hook).observe_exit(
            lease=self._lease_for_envelope(envelope),
            now=now,
        )
        self._leases.ingest_result(
            envelope=envelope,
            now=now,
            expected_attempt_version=attempt.aggregate_version,
        )
        self._fault("result_ingested")
        self._set_status(
            request=request,
            status=envelope.terminal_result,
            event_type="runtime_node.completed",
            epoch=envelope.epoch,
            now=now,
        )
        reservation = self._workspaces.read_reservation(request.repository_id)
        if reservation is None or reservation.run_id != request.run_id:
            raise InternalInvariantViolationError(
                "worker result has no matching workspace reservation"
            )
        self._workspaces.cleanup(reservation, epoch=envelope.epoch, now=now)

    def enter_human_wait(
        self,
        *,
        dispatched: DispatchedFixture,
        reason: str,
        now: datetime,
    ) -> None:
        """Stop the child, retain its workspace, and fence the waiting attempt."""
        if not reason.strip():
            raise InvalidInputError("human-wait reason must be non-blank")
        request = self._request_for(dispatched.lease.run_id, dispatched.lease.node_id)
        WorkerSupervisor(self._ledger, self._coordinator, fault_hook=self._fault_hook).cancel(
            lease=dispatched.lease,
            identity=dispatched.identity,
            now=now,
        )
        RecoveryCoordinator(self._ledger, self._coordinator).reconcile(
            lease=dispatched.lease,
            workspace_path=dispatched.workspace.workspace_path,
            epoch=dispatched.lease.epoch,
            now=now,
        )
        envelope = WorkerResultEnvelope(
            run_id=dispatched.lease.run_id,
            node_id=dispatched.lease.node_id,
            attempt_id=dispatched.lease.attempt_id,
            epoch=dispatched.lease.epoch,
            fencing_token=dispatched.lease.fencing_token,
            operation_id=dispatched.lease.operation_id,
            terminal_result="cancelled",
            artifact_references=(),
            result={"human_wait_reason": reason},
        )
        attempt = self._ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=dispatched.lease.attempt_id
        )
        if attempt is None:
            raise InternalInvariantViolationError("human wait has no durable node attempt")
        self._leases.ingest_result(
            envelope=envelope,
            now=now,
            expected_attempt_version=attempt.aggregate_version,
        )
        retained = self._workspaces.retain(
            dispatched.workspace,
            epoch=dispatched.lease.epoch,
            now=now,
        )
        self._set_status(
            request=request,
            status="awaiting_human",
            event_type="runtime_node.human_wait_entered",
            epoch=dispatched.lease.epoch,
            now=now,
            extra={"human_wait_reason": reason, "workspace_status": retained.status},
        )
        self._fault("human_wait_entered")

    def resume_human_wait(
        self,
        *,
        request: FixtureDispatch,
        epoch: int,
        now: datetime,
    ) -> None:
        """Replace a waiting attempt only with a new attempt and operation ID."""
        prior = self._request_for(request.run_id, request.node_id)
        state = _runtime_state(self._ledger, prior)
        if state.get("status") != "awaiting_human":
            raise ExternalConflictError("only a human-waiting node can be resumed")
        if request.attempt_id == prior.attempt_id or request.operation_id == prior.operation_id:
            raise InvalidInputError("human-wait resume requires a fresh attempt and operation ID")
        reservation = self._workspaces.read_reservation(request.repository_id)
        if reservation is None or reservation.run_id != request.run_id:
            raise InternalInvariantViolationError(
                "human-wait resume has no matching workspace reservation"
            )
        self._workspaces.resume(reservation, epoch=epoch, now=now)
        self._replace_request(
            request=request,
            epoch=epoch,
            now=now,
            event_type="runtime_node.human_wait_resumed",
        )
        self._fault("human_wait_resumed")

    def _acquire_or_renew(self, *, now: datetime, heartbeat_window: timedelta) -> CoordinatorEpoch:
        current = self._coordinator.current_epoch()
        if current is None or not current.active_at(now):
            return self._coordinator.acquire(now=now, heartbeat_window=heartbeat_window)
        if current.owner != self._coordinator.owner:
            raise ExternalConflictError("another coordinator owns the active ledger epoch")
        return self._coordinator.renew(
            epoch=current.epoch,
            now=now,
            heartbeat_window=heartbeat_window,
        )

    def _register(self, *, request: FixtureDispatch, epoch: int, now: datetime) -> None:
        projection = self._ledger.read_projection(
            aggregate_type=_RUNTIME_NODE, aggregate_id=_node_id(request)
        )
        if projection is not None:
            current = _request_from_state(projection.state)
            if current != request:
                raise ExternalConflictError(
                    "runtime node already exists with different immutable request"
                )
            return
        state = _request_state(request, status="queued")
        self._ledger.append(
            AppendCommand(
                correlation_id=f"runtime-node-register:{_node_id(request)}",
                events=(
                    EventWrite(
                        aggregate_type=_RUNTIME_NODE,
                        aggregate_id=_node_id(request),
                        expected_version=0,
                        event_type="runtime_node.queued",
                        schema_version=1,
                        payload=state,
                    ),
                ),
                process_manager_updates=(self._coordinator.epoch_guard(epoch=epoch, now=now),),
            )
        )

    def _replace_request(
        self, *, request: FixtureDispatch, epoch: int, now: datetime, event_type: str
    ) -> None:
        projection = self._ledger.read_projection(
            aggregate_type=_RUNTIME_NODE, aggregate_id=_node_id(request)
        )
        if projection is None:
            raise InternalInvariantViolationError("cannot replace an unknown runtime node")
        state = _request_state(request, status="queued")
        self._ledger.append(
            AppendCommand(
                correlation_id=f"runtime-node-resume:{_node_id(request)}:{request.attempt_id}",
                events=(
                    EventWrite(
                        aggregate_type=_RUNTIME_NODE,
                        aggregate_id=_node_id(request),
                        expected_version=projection.aggregate_version,
                        event_type=event_type,
                        schema_version=1,
                        payload=state,
                    ),
                ),
                process_manager_updates=(self._coordinator.epoch_guard(epoch=epoch, now=now),),
            )
        )

    def _set_status(
        self,
        *,
        request: FixtureDispatch,
        status: str,
        event_type: str,
        epoch: int,
        now: datetime,
        extra: dict[str, object] | None = None,
    ) -> None:
        projection = self._ledger.read_projection(
            aggregate_type=_RUNTIME_NODE, aggregate_id=_node_id(request)
        )
        if projection is None:
            raise InternalInvariantViolationError("runtime node state is missing")
        state = dict(projection.state)
        state["status"] = status
        if extra is not None:
            state.update(extra)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"{event_type}:{_node_id(request)}:{projection.aggregate_version}",
                events=(
                    EventWrite(
                        aggregate_type=_RUNTIME_NODE,
                        aggregate_id=_node_id(request),
                        expected_version=projection.aggregate_version,
                        event_type=event_type,
                        schema_version=1,
                        payload=state,
                    ),
                ),
                process_manager_updates=(self._coordinator.epoch_guard(epoch=epoch, now=now),),
            )
        )

    def _request_for(self, run_id: str, node_id: str) -> FixtureDispatch:
        projection = self._ledger.read_projection(
            aggregate_type=_RUNTIME_NODE, aggregate_id=f"{run_id}:{node_id}"
        )
        if projection is None:
            raise ExternalConflictError("worker result references an unknown runtime node")
        return _request_from_state(projection.state)

    def _requests_from_ledger(self) -> dict[NodeKey, FixtureDispatch]:
        requests: dict[NodeKey, FixtureDispatch] = {}
        for projection in self._ledger.list_projections(aggregate_type=_RUNTIME_NODE):
            request = _request_from_state(projection.state)
            requests[NodeKey(request.run_id, request.node_id)] = request
        return requests

    def _lease_for_envelope(self, envelope: WorkerResultEnvelope) -> FencedNodeLease:
        record = self._ledger.read_lease(run_id=envelope.run_id, node_id=envelope.node_id)
        if record is None or record.expires_at is None:
            raise InternalInvariantViolationError("worker result has no durable active lease")
        try:
            expires_at = datetime.fromisoformat(record.expires_at)
        except ValueError as error:
            raise InternalInvariantViolationError("worker lease expiry is invalid") from error
        return FencedNodeLease(
            run_id=record.run_id,
            node_id=record.node_id,
            attempt_id=record.attempt_id,
            epoch=record.epoch,
            fencing_token=record.fencing_token,
            operation_id=envelope.operation_id,
            owner=record.owner,
            expires_at=expires_at,
        )

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)


def _node_id(request: FixtureDispatch) -> str:
    return f"{request.run_id}:{request.node_id}"


def _request_state(request: FixtureDispatch, *, status: str) -> dict[str, object]:
    return {
        "run_id": request.run_id,
        "node_id": request.node_id,
        "attempt_id": request.attempt_id,
        "repository_id": request.repository_id,
        "repository_path": str(request.repository_path),
        "workspace_path": str(request.workspace_path),
        "base_revision": request.base_revision,
        "command": list(request.command),
        "expected_attempt_version": request.expected_attempt_version,
        "operation_id": request.operation_id,
        "dependencies": [[run_id, node_id] for run_id, node_id in request.dependencies],
        "workflow_definition_id": request.workflow_definition_id,
        "status": status,
    }


def _request_from_state(state: object) -> FixtureDispatch:
    if not isinstance(state, dict):
        raise InternalInvariantViolationError("runtime node state is invalid")
    run_id = _required_text(state, "run_id")
    node_id = _required_text(state, "node_id")
    attempt_id = _required_text(state, "attempt_id")
    repository_id = _required_text(state, "repository_id")
    repository_path = _required_text(state, "repository_path")
    workspace_path = _required_text(state, "workspace_path")
    base_revision = _required_text(state, "base_revision")
    operation_id = _required_text(state, "operation_id")
    command = state.get("command")
    expected_attempt_version = state.get("expected_attempt_version")
    dependencies = state.get("dependencies")
    workflow_definition_id = state.get("workflow_definition_id")
    if (
        not isinstance(command, list)
        or not all(isinstance(value, str) and value for value in command)
        or not isinstance(expected_attempt_version, int)
        or expected_attempt_version < 0
        or not isinstance(dependencies, list)
    ):
        raise InternalInvariantViolationError("runtime node state has invalid execution data")
    if workflow_definition_id is not None and (
        not isinstance(workflow_definition_id, str) or not workflow_definition_id.strip()
    ):
        raise InternalInvariantViolationError("runtime node state has invalid workflow definition")
    parsed_dependencies: list[tuple[str, str]] = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, list)
            or len(dependency) != 2
            or not all(isinstance(value, str) and value.strip() for value in dependency)
        ):
            raise InternalInvariantViolationError("runtime node state has invalid dependencies")
        first, second = dependency
        assert isinstance(first, str)
        assert isinstance(second, str)
        parsed_dependencies.append((first, second))
    return FixtureDispatch(
        run_id=run_id,
        node_id=node_id,
        attempt_id=attempt_id,
        repository_id=repository_id,
        repository_path=Path(repository_path),
        workspace_path=Path(workspace_path),
        base_revision=base_revision,
        command=tuple(command),
        expected_attempt_version=expected_attempt_version,
        operation_id=operation_id,
        dependencies=tuple(parsed_dependencies),
        workflow_definition_id=workflow_definition_id,
    )


def _required_text(state: dict[object, object], field_name: str) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InternalInvariantViolationError(
            "runtime node state has incomplete identifiers",
            details={"field": field_name},
        )
    return value


def _runtime_state(ledger: LedgerService, request: FixtureDispatch) -> dict[str, object]:
    projection = ledger.read_projection(
        aggregate_type=_RUNTIME_NODE,
        aggregate_id=_node_id(request),
    )
    if projection is None:
        return {"status": "queued"}
    return dict(projection.state)


def _schedulable(request: FixtureDispatch, *, state: dict[str, object]) -> SchedulableNode:
    status = state.get("status")
    if not isinstance(status, str):
        raise InternalInvariantViolationError("runtime node status is invalid")
    states = {
        "queued": SchedulableState.QUEUED,
        "running": SchedulableState.RUNNING,
        "awaiting_human": SchedulableState.AWAITING_HUMAN,
        "passed": SchedulableState.SUCCEEDED,
        "failed": SchedulableState.FAILED,
        "cancelled": SchedulableState.CANCELLED,
        "blocked": SchedulableState.BLOCKED,
    }
    if status not in states:
        raise InternalInvariantViolationError("runtime node status is unknown")
    return SchedulableNode(
        key=NodeKey(request.run_id, request.node_id),
        dependencies=tuple(NodeKey(run_id, node_id) for run_id, node_id in request.dependencies),
        state=states[status],
        repository_id=request.repository_id,
    )


__all__ = [
    "CoordinatorRuntime",
    "DispatchedFixture",
    "FixtureDispatch",
    "FixtureRuntime",
    "RuntimeTickResult",
]
