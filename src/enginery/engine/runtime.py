"""Coordinator-owned runtime scheduling and worker-result ingestion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.domain.ids import NodeId
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.domain.workflow.node import ActorType
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

RUNTIME_NODE_AGGREGATE_TYPE = "runtime_node"
RUN_AGGREGATE_TYPE = "run"


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
    retain_workspace: bool = False

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
class WorkflowDispatch:
    """One manifest-bound agent node delegated to the coordinator runtime."""

    request: FixtureDispatch
    manifest: WorkflowManifest

    def __post_init__(self) -> None:
        if self.request.workflow_definition_id != self.manifest.id.value:
            raise InvalidInputError("workflow dispatch request must bind its manifest identity")
        node = self.manifest.nodes.get(NodeId(self.request.node_id))
        if node is None:
            raise InvalidInputError("workflow dispatch references an unknown manifest node")
        _require_manifest_dependencies(self.request, node.dependencies)
        if node.actor_type is not ActorType.AGENT:
            raise InvalidInputError("workflow dispatch requires an agent-task manifest node")


@dataclass(frozen=True, slots=True)
class WorkflowNodeDispatch:
    """One manifest-bound deterministic or human node owned by the runtime."""

    request: FixtureDispatch
    manifest: WorkflowManifest

    def __post_init__(self) -> None:
        if self.request.workflow_definition_id != self.manifest.id.value:
            raise InvalidInputError("workflow node request must bind its manifest identity")
        node = self.manifest.nodes.get(NodeId(self.request.node_id))
        if node is None:
            raise InvalidInputError("workflow node dispatch references an unknown manifest node")
        _require_manifest_dependencies(self.request, node.dependencies)
        if node.actor_type is ActorType.AGENT:
            raise InvalidInputError("workflow node dispatch requires a non-agent manifest node")

    @property
    def actor_type(self) -> ActorType:
        """Return the manifest-declared owner of this node."""
        return self.manifest.nodes[NodeId(self.request.node_id)].actor_type


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

    def recover_dispatched(
        self, *, lease: FencedNodeLease, workspace: WorkspaceReservation
    ) -> DispatchedFixture:
        """Reconstruct a dispatched fixture from durable worker and workspace state."""
        return DispatchedFixture(
            lease=lease,
            identity=self._supervisor.identity_for(lease=lease),
            workspace=workspace,
        )

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

    def claim_epoch(self, *, now: datetime, heartbeat_window: timedelta) -> CoordinatorEpoch:
        """Acquire or renew the coordinator epoch for an operator lifecycle command."""
        return self._acquire_or_renew(now=now, heartbeat_window=heartbeat_window)

    def read_node_request(self, *, run_id: str, node_id: str) -> FixtureDispatch:
        """Return the current durable request for one runtime node."""
        return self._request_for(run_id, node_id)

    def recover_dispatched(self, *, run_id: str, node_id: str) -> DispatchedFixture:
        """Reconstruct one dispatched fixture after coordinator replacement."""
        request = self._request_for(run_id, node_id)
        return self._fixture_runtime.recover_dispatched(
            lease=self._active_lease(request),
            workspace=self._require_workspace(request),
        )

    def register_run(
        self,
        *,
        run_id: str,
        initial_state: Mapping[str, object],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> CoordinatorEpoch:
        """Persist an immutable workflow-run intent under coordinator fencing."""
        if not run_id.strip():
            raise InvalidInputError("run_id must be non-blank")
        state = dict(initial_state)
        if state.get("run_id") != run_id:
            raise InvalidInputError("initial run state must bind its run_id")
        epoch = self._acquire_or_renew(now=now, heartbeat_window=heartbeat_window)
        projection = self._ledger.read_projection(
            aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=run_id
        )
        if projection is None:
            self._ledger.append(
                AppendCommand(
                    correlation_id=f"run-register:{run_id}",
                    events=(
                        EventWrite(
                            aggregate_type=RUN_AGGREGATE_TYPE,
                            aggregate_id=run_id,
                            expected_version=0,
                            event_type="run.created",
                            schema_version=1,
                            payload=state,
                        ),
                    ),
                    process_manager_updates=(
                        self._coordinator.epoch_guard(epoch=epoch.epoch, now=now),
                    ),
                )
            )
            return epoch
        if dict(projection.state) != state:
            raise ExternalConflictError(
                "run already exists with a different immutable request",
                details={"run_id": run_id},
            )
        return epoch

    def register_node(
        self,
        *,
        dispatch: WorkflowNodeDispatch,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> CoordinatorEpoch:
        self._require_completed_dependencies(dispatch.request)
        epoch = self._acquire_or_renew(now=now, heartbeat_window=heartbeat_window)
        self._register(
            request=dispatch.request,
            actor_type=dispatch.actor_type,
            epoch=epoch.epoch,
            now=now,
        )
        return epoch

    def retry_workflow_node(
        self,
        *,
        dispatch: WorkflowNodeDispatch,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> CoordinatorEpoch:
        """Replace a terminal manifest node with its next fenced attempt."""
        epoch = self._acquire_or_renew(now=now, heartbeat_window=heartbeat_window)
        self.retry_node(
            request=dispatch.request,
            actor_type=dispatch.actor_type,
            epoch=epoch.epoch,
            now=now,
        )
        return epoch

    def complete_node(
        self,
        *,
        run_id: str,
        node_id: str,
        epoch: int,
        now: datetime,
        outcome: str = "passed",
        extra: dict[str, object] | None = None,
    ) -> None:
        """Persist the outcome of a non-worker manifest node."""
        if outcome not in {"passed", "failed", "cancelled", "blocked"}:
            raise InvalidInputError("runtime node outcome is unsupported")
        request = self._request_for(run_id, node_id)
        state = _runtime_state(self._ledger, request)
        _require_non_agent_node(state)
        if state.get("status") != "queued":
            raise ExternalConflictError("only a queued deterministic node can complete")
        self._set_status(
            request=request,
            status=outcome,
            event_type="runtime_node.deterministic_completed",
            epoch=epoch,
            now=now,
            extra=extra,
        )

    def await_human_node(
        self,
        *,
        run_id: str,
        node_id: str,
        epoch: int,
        now: datetime,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Persist a deterministic workflow node that requires a human decision."""
        if not reason.strip():
            raise InvalidInputError("human-wait reason must be non-blank")
        request = self._request_for(run_id, node_id)
        state = _runtime_state(self._ledger, request)
        _require_non_agent_node(state)
        if state.get("status") != "queued":
            raise ExternalConflictError("only a queued deterministic node can await human input")
        details: dict[str, object] = {"human_wait_reason": reason}
        if extra is not None:
            details.update(extra)
        self._set_status(
            request=request,
            status="awaiting_human",
            event_type="runtime_node.deterministic_human_wait",
            epoch=epoch,
            now=now,
            extra=details,
        )

    def resolve_human_wait(
        self,
        *,
        run_id: str,
        node_id: str,
        epoch: int,
        now: datetime,
        outcome: str,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Record an authenticated terminal decision for a human-waiting node."""
        if outcome not in {"passed", "failed", "cancelled", "blocked"}:
            raise InvalidInputError("runtime node outcome is unsupported")
        request = self._request_for(run_id, node_id)
        state = _runtime_state(self._ledger, request)
        _require_non_agent_node(state)
        if state.get("status") != "awaiting_human":
            raise ExternalConflictError("only a human-waiting node can be resolved")
        self._set_status(
            request=request,
            status=outcome,
            event_type="runtime_node.human_wait_resolved",
            epoch=epoch,
            now=now,
            extra=extra,
        )

    def retry_node(
        self,
        *,
        request: FixtureDispatch,
        actor_type: ActorType,
        epoch: int,
        now: datetime,
    ) -> None:
        """Replace a terminal manifest node with a fresh fenced attempt."""
        prior = self._request_for(request.run_id, request.node_id)
        state = _runtime_state(self._ledger, prior)
        if _actor_type_from_state(state) is not actor_type:
            raise ExternalConflictError("runtime node retry changes its actor type")
        if state.get("status") not in {"passed", "failed", "cancelled", "blocked"}:
            raise ExternalConflictError("only a terminal runtime node can be retried")
        if request.attempt_id == prior.attempt_id or request.operation_id == prior.operation_id:
            raise InvalidInputError("runtime node retry requires a fresh attempt and operation ID")
        if (
            replace(
                request,
                attempt_id=prior.attempt_id,
                operation_id=prior.operation_id,
            )
            != prior
        ):
            raise ExternalConflictError("runtime node retry changes immutable request fields")
        self._require_completed_dependencies(request)
        self._replace_request(
            request=request,
            epoch=epoch,
            now=now,
            event_type="runtime_node.retried",
        )
        self._fault("node_retried")

    def tick(
        self,
        *,
        now: datetime,
        heartbeat_window: timedelta,
        lease_window: timedelta,
        limits: SchedulingLimits,
        requests: tuple[FixtureDispatch | WorkflowDispatch, ...] = (),
    ) -> RuntimeTickResult:
        """Consume commands, derive durable readiness, and launch a fair batch."""
        epoch = self._acquire_or_renew(now=now, heartbeat_window=heartbeat_window)
        self._fault("epoch_acquired")
        consumed = self._coordinator.consume_pending(
            epoch=epoch.epoch,
            now=now,
            heartbeat_window=heartbeat_window,
        )
        for dispatch in requests:
            request = _fixture_request(dispatch)
            self._register(
                request=request,
                actor_type=_actor_type(dispatch),
                epoch=epoch.epoch,
                now=now,
            )
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
        """Validate and ingest an exact current-lease worker result."""
        request = self._request_for(envelope.run_id, envelope.node_id)
        _require_agent_node(_runtime_state(self._ledger, request))
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
        if request.retain_workspace:
            self._workspaces.retain(reservation, epoch=envelope.epoch, now=now)
        else:
            self._workspaces.cleanup(reservation, epoch=envelope.epoch, now=now)

    def verify_implementation_branch(self, *, run_id: str, head_branch: str) -> str:
        """Verify the retained implementation workspace is ready for its configured PR branch."""
        request = self._request_for(run_id, "implement")
        reservation = self._require_workspace(request)
        return self._workspaces.verify_implementation_branch(reservation, head_branch=head_branch)

    def release_workspace(
        self,
        *,
        run_id: str,
        repository_id: str,
        epoch: int,
        now: datetime,
    ) -> WorkspaceReservation:
        """Release one retained run workspace through the current coordinator epoch."""
        reservation = self._workspaces.read_reservation(repository_id)
        if reservation is None or reservation.run_id != run_id:
            raise InternalInvariantViolationError(
                "workspace release has no matching workspace reservation"
            )
        if reservation.status != "retained":
            raise ExternalConflictError("workspace release requires a retained workspace")
        return self._workspaces.cleanup(reservation, epoch=epoch, now=now)

    def cancel_node(self, *, run_id: str, node_id: str, epoch: int, now: datetime) -> None:
        """Cancel a queued, running, or human-waiting node through durable state."""
        request = self._request_for(run_id, node_id)
        state = _runtime_state(self._ledger, request)
        status = state.get("status")
        transition_epoch = epoch
        if status == "running":
            lease = self._active_lease(request)
            if epoch != lease.epoch:
                raise ExternalConflictError(
                    "cancellation epoch does not match the current node lease"
                )
            transition_epoch = lease.epoch
            WorkerSupervisor(
                self._ledger, self._coordinator, fault_hook=self._fault
            ).cancel_persisted(lease=lease, now=now)
            reservation = self._require_workspace(request)
            assessment = RecoveryCoordinator(self._ledger, self._coordinator).reconcile(
                lease=lease,
                workspace_path=reservation.workspace_path,
                epoch=transition_epoch,
                now=now,
            )
            attempt = self._ledger.read_projection(
                aggregate_type="node_attempt", aggregate_id=request.attempt_id
            )
            if attempt is None:
                raise InternalInvariantViolationError("cancellation has no durable node attempt")
            self._leases.ingest_result(
                envelope=WorkerResultEnvelope(
                    run_id=run_id,
                    node_id=node_id,
                    attempt_id=request.attempt_id,
                    epoch=lease.epoch,
                    fencing_token=lease.fencing_token,
                    operation_id=request.operation_id,
                    terminal_result="cancelled",
                    artifact_references=(),
                    result={"cancellation": "operator_requested"},
                ),
                now=now,
                expected_attempt_version=attempt.aggregate_version,
                allow_expired_cancellation=True,
            )
            if not assessment.ready_to_release:
                self._set_status(
                    request=request,
                    status="awaiting_human",
                    event_type="runtime_node.cancellation_cleanup_pending",
                    epoch=transition_epoch,
                    now=now,
                    extra={"cancellation_cleanup_reason": assessment.reason},
                )
                self._fault("node_cancellation_cleanup_pending")
                return
            self._workspaces.cleanup(reservation, epoch=transition_epoch, now=now)
        elif status == "awaiting_human":
            if _actor_type_from_state(state) is not ActorType.AGENT:
                raise ExternalConflictError("cannot cancel a human-waiting non-agent node")
            lease = self._active_lease(request)
            if epoch != lease.epoch:
                raise ExternalConflictError(
                    "cancellation epoch does not match the current node lease"
                )
            transition_epoch = lease.epoch
            self._workspaces.cleanup(
                self._require_workspace(request), epoch=transition_epoch, now=now
            )
        elif status != "queued":
            raise ExternalConflictError(
                "only queued, running, or human-waiting nodes can be cancelled"
            )
        self._set_status(
            request=request,
            status="cancelled",
            event_type="runtime_node.cancelled",
            epoch=transition_epoch,
            now=now,
        )
        self._fault("node_cancelled")

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
        if _actor_type_from_state(state) is ActorType.AGENT:
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

    def _require_completed_dependencies(self, request: FixtureDispatch) -> None:
        for run_id, node_id in request.dependencies:
            projection = self._ledger.read_projection(
                aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:{node_id}"
            )
            if projection is None or projection.state.get("status") != "passed":
                raise ExternalConflictError(
                    "workflow node dependencies are not completed",
                    details={"run_id": run_id, "node_id": node_id},
                )

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

    def _register(
        self,
        *,
        request: FixtureDispatch,
        actor_type: ActorType,
        epoch: int,
        now: datetime,
    ) -> None:
        projection = self._ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=_node_id(request)
        )
        if projection is not None:
            if _actor_type_from_state(projection.state) is not actor_type:
                raise ExternalConflictError("runtime node already exists with different actor type")
            current = _request_from_state(projection.state)
            if current != request:
                raise ExternalConflictError(
                    "runtime node already exists with different immutable request"
                )
            return
        state = _request_state(request, status="queued", actor_type=actor_type)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"runtime-node-register:{_node_id(request)}",
                events=(
                    EventWrite(
                        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
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
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=_node_id(request)
        )
        if projection is None:
            raise InternalInvariantViolationError("cannot replace an unknown runtime node")
        state = _request_state(
            request,
            status="queued",
            actor_type=_actor_type_from_state(projection.state),
        )
        self._ledger.append(
            AppendCommand(
                correlation_id=f"{event_type}:{_node_id(request)}:{request.attempt_id}",
                events=(
                    EventWrite(
                        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
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
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=_node_id(request)
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
                        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
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
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:{node_id}"
        )
        if projection is None:
            raise ExternalConflictError("worker result references an unknown runtime node")
        return _request_from_state(projection.state)

    def _requests_from_ledger(self) -> dict[NodeKey, FixtureDispatch]:
        requests: dict[NodeKey, FixtureDispatch] = {}
        for projection in self._ledger.list_projections(aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE):
            request = _request_from_state(projection.state)
            requests[NodeKey(request.run_id, request.node_id)] = request
        return requests

    def _active_lease(self, request: FixtureDispatch) -> FencedNodeLease:
        record = self._ledger.read_lease(run_id=request.run_id, node_id=request.node_id)
        if record is None or record.expires_at is None:
            raise InternalInvariantViolationError("runtime node has no durable active lease")
        try:
            expires_at = datetime.fromisoformat(record.expires_at)
        except ValueError as error:
            raise InternalInvariantViolationError("runtime node lease expiry is invalid") from error
        return FencedNodeLease(
            run_id=record.run_id,
            node_id=record.node_id,
            attempt_id=record.attempt_id,
            epoch=record.epoch,
            fencing_token=record.fencing_token,
            operation_id=request.operation_id,
            owner=record.owner,
            expires_at=expires_at,
        )

    def _require_workspace(self, request: FixtureDispatch) -> WorkspaceReservation:
        reservation = self._workspaces.read_reservation(request.repository_id)
        if reservation is None or reservation.run_id != request.run_id:
            raise InternalInvariantViolationError(
                "runtime node has no matching workspace reservation"
            )
        return reservation

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


def _actor_type(dispatch: FixtureDispatch | WorkflowDispatch) -> ActorType:
    del dispatch
    return ActorType.AGENT


def _fixture_request(dispatch: FixtureDispatch | WorkflowDispatch) -> FixtureDispatch:
    if isinstance(dispatch, WorkflowDispatch):
        return dispatch.request
    return dispatch


def _node_id(request: FixtureDispatch) -> str:
    return f"{request.run_id}:{request.node_id}"


def _request_state(
    request: FixtureDispatch, *, status: str, actor_type: ActorType
) -> dict[str, object]:
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
        "retain_workspace": request.retain_workspace,
        "actor_type": actor_type.value,
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
    retain_workspace = state.get("retain_workspace", False)
    if not isinstance(retain_workspace, bool):
        raise InternalInvariantViolationError("runtime node state has invalid workspace retention")
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
        retain_workspace=retain_workspace,
    )


def _actor_type_from_state(state: object) -> ActorType:
    if not isinstance(state, dict):
        raise InternalInvariantViolationError("runtime node state is invalid")
    actor_type = state.get("actor_type")
    if actor_type is None:
        return ActorType.AGENT
    if not isinstance(actor_type, str):
        raise InternalInvariantViolationError("runtime node actor type is invalid")
    try:
        return ActorType(actor_type)
    except ValueError as error:
        raise InternalInvariantViolationError("runtime node actor type is invalid") from error


def _require_agent_node(state: object) -> None:
    if _actor_type_from_state(state) is not ActorType.AGENT:
        raise ExternalConflictError("worker operation requires an agent-owned runtime node")


def _require_non_agent_node(state: object) -> None:
    if _actor_type_from_state(state) is ActorType.AGENT:
        raise ExternalConflictError("deterministic operation requires a non-agent runtime node")


def _required_text(state: dict[object, object], field_name: str) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InternalInvariantViolationError(
            "runtime node state has incomplete identifiers",
            details={"field": field_name},
        )
    return value


def _require_manifest_dependencies(
    request: FixtureDispatch, dependencies: tuple[NodeId, ...]
) -> None:
    expected = {(request.run_id, str(dependency)) for dependency in dependencies}
    if len(request.dependencies) != len(expected) or set(request.dependencies) != expected:
        raise InvalidInputError("workflow dispatch dependencies do not match its manifest node")


def _runtime_state(ledger: LedgerService, request: FixtureDispatch) -> dict[str, object]:
    projection = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
        aggregate_id=_node_id(request),
    )
    if projection is None:
        return {"status": "queued"}
    return dict(projection.state)


def _schedulable(request: FixtureDispatch, *, state: dict[str, object]) -> SchedulableNode:
    status = state.get("status")
    if not isinstance(status, str):
        raise InternalInvariantViolationError("runtime node status is invalid")
    actor_type = _actor_type_from_state(state)
    if actor_type is not ActorType.AGENT and status == "queued":
        status = "blocked"
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
    "RUNTIME_NODE_AGGREGATE_TYPE",
    "RUN_AGGREGATE_TYPE",
    "CoordinatorRuntime",
    "DispatchedFixture",
    "FixtureDispatch",
    "FixtureRuntime",
    "RuntimeTickResult",
    "WorkflowDispatch",
]
