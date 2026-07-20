"""``PlanExecutionCoordinator``: the plan-to-child-run process manager.

Turns a validated ``Plan`` into linked child ``Run``s through the same
coordinator-owned, fenced, crash-safe machinery every other workflow node
uses. It owns exactly two responsibilities: fan-out (creating a new child
run for every milestone whose plan-level dependencies have all succeeded)
and join (rolling up every milestone's recorded outcome into the plan's
own terminal or in-progress state). It never decides *how* a milestone's
work is performed -- that remains whatever workflow the caller's
``child_run_state`` builder binds the new run to, exactly as
``CoordinatorRuntime.register_run`` already treats a run's initial state
as opaque.

Idempotent fan-out after a coordinator crash: each child run's identity
is derived deterministically from the plan's content digest and the
milestone's id (``derive_child_run_id``), never generated fresh per call.
A crash between registering the child ``Run`` and recording that
registration in the ``PlanExecution`` projection is therefore always
safe to retry -- the second attempt derives the same run id, finds the
run already registered (a no-op through
``CoordinatorRuntime.register_run``'s own idempotent read-before-write
check), and only then completes the missing ``PlanExecution``-side
record. No branch of this recovery path can create a second run for the
same milestone. ``child_run_state`` must be a pure function of its
arguments and must bind ``"run_id": str(run_id)`` in its returned
mapping, matching ``CoordinatorRuntime.register_run``'s own contract --
otherwise a retried call would not observe the exact-match it needs to
recognize an already-registered child as a no-op.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from enginery.domain.errors import ExternalConflictError, InternalInvariantViolationError
from enginery.domain.ids import PlanExecutionId, PlanMilestoneId, RunId
from enginery.domain.plan_execution import MilestoneRunLink, MilestoneRunState, PlanExecution
from enginery.domain.serialization import plan_execution_from_dict, plan_execution_to_dict
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.plans.model import Plan

PLAN_EXECUTION_AGGREGATE_TYPE = "plan_execution"

ChildRunStateBuilder = Callable[[Plan, PlanMilestoneId, RunId], Mapping[str, object]]


def derive_child_run_id(plan: Plan, milestone_id: PlanMilestoneId) -> RunId:
    """Deterministically derive the child run id for one milestone of ``plan``.

    A pure function of the plan's content digest, the plan's own id, and
    the milestone id: the same plan always derives the same child run id
    for the same milestone, which is what makes fan-out idempotent across
    a coordinator restart.
    """
    payload = "\x1f".join((str(plan.content_digest), str(plan.id), str(milestone_id)))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return RunId(f"plan-child-{digest}")


class PlanExecutionCoordinator:
    """Coordinator-owned fan-out and join for one running plan instance."""

    def __init__(self, ledger: LedgerService, runtime: CoordinatorRuntime) -> None:
        self._ledger = ledger
        self._runtime = runtime

    def read(self, plan_execution_id: PlanExecutionId) -> PlanExecution | None:
        """Read the current durable projection.

        The returned object's ``aggregate_version`` always comes from the
        ledger's own tracked version for this aggregate, never from the
        self-serialized payload field, so it can never drift from what
        ``append``'s optimistic-concurrency check actually enforces.
        """
        projection = self._ledger.read_projection(
            aggregate_type=PLAN_EXECUTION_AGGREGATE_TYPE, aggregate_id=str(plan_execution_id)
        )
        if projection is None:
            return None
        execution = plan_execution_from_dict(dict(projection.state))
        if execution.aggregate_version != projection.aggregate_version:
            execution = replace(execution, aggregate_version=projection.aggregate_version)
        return execution

    def start(
        self,
        plan: Plan,
        *,
        plan_execution_id: PlanExecutionId,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> PlanExecution:
        """Idempotently create the durable fan-out/join record for ``plan``."""
        existing = self.read(plan_execution_id)
        initial = PlanExecution.initial(
            plan_execution_id=plan_execution_id,
            plan_id=plan.id,
            plan_digest=plan.content_digest,
            milestone_ids=plan.topological_order(),
        )
        if existing is not None:
            if existing.plan_id != initial.plan_id or existing.plan_digest != initial.plan_digest:
                raise ExternalConflictError(
                    "plan execution already exists for a different plan",
                    details={"plan_execution_id": str(plan_execution_id)},
                )
            return existing
        epoch = self._runtime.claim_epoch(now=now, heartbeat_window=heartbeat_window)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"plan-execution-start:{plan_execution_id}",
                events=(
                    EventWrite(
                        aggregate_type=PLAN_EXECUTION_AGGREGATE_TYPE,
                        aggregate_id=str(plan_execution_id),
                        expected_version=0,
                        event_type="plan_execution.started",
                        schema_version=1,
                        payload=plan_execution_to_dict(initial),
                    ),
                ),
                process_manager_updates=(
                    self._runtime.coordinator.epoch_guard(epoch=epoch.epoch, now=now),
                ),
            )
        )
        return self._require(plan_execution_id)

    def fan_out(
        self,
        plan: Plan,
        plan_execution_id: PlanExecutionId,
        *,
        now: datetime,
        heartbeat_window: timedelta,
        child_run_state: ChildRunStateBuilder,
    ) -> PlanExecution:
        """Idempotently register a child run for every ready, unregistered milestone.

        A milestone is ready when every one of its plan-level dependencies
        has reached ``MilestoneRunState.SUCCEEDED``. Independent milestones
        (no dependency relationship between them) are registered in the
        same call, matching "independent milestones run concurrently".
        """
        current = self._require(plan_execution_id)
        for milestone_id in plan.topological_order():
            link = current.link(milestone_id)
            if link.state is not MilestoneRunState.PENDING:
                continue
            if not _dependencies_succeeded(plan, milestone_id, current):
                continue
            run_id = derive_child_run_id(plan, milestone_id)
            state = dict(child_run_state(plan, milestone_id, run_id))
            self._runtime.register_run(
                run_id=str(run_id),
                initial_state=state,
                now=now,
                heartbeat_window=heartbeat_window,
            )
            current = self._record(
                current,
                current.link(milestone_id).transition_to(
                    MilestoneRunState.REGISTERED, run_id=run_id
                ),
                now=now,
                heartbeat_window=heartbeat_window,
            )
        return current

    def record_milestone_outcome(
        self,
        plan_execution_id: PlanExecutionId,
        milestone_id: PlanMilestoneId,
        outcome: MilestoneRunState,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> PlanExecution:
        """Idempotently record one milestone's terminal (or cancelled) outcome.

        Re-recording the same terminal outcome for an already-terminal
        milestone is a no-op rather than an error, so a resumed process
        manager can safely re-observe a child run's terminal state without
        tracking what it already recorded.
        """
        current = self._require(plan_execution_id)
        existing = current.link(milestone_id)
        if existing.state is outcome:
            return current
        updated_link = existing.transition_to(outcome)
        return self._record(current, updated_link, now=now, heartbeat_window=heartbeat_window)

    def cancel_unregistered(
        self,
        plan_execution_id: PlanExecutionId,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> PlanExecution:
        """Cancel every milestone that has not yet been registered as a child run.

        A milestone already registered continues under its own run's
        lifecycle; cancelling it is the caller's responsibility through the
        existing node-level cancellation path, then recorded here through
        :meth:`record_milestone_outcome`.
        """
        current = self._require(plan_execution_id)
        for link in list(current.milestones.values()):
            if link.state is MilestoneRunState.PENDING:
                current = self._record(
                    current,
                    link.transition_to(MilestoneRunState.CANCELLED),
                    now=now,
                    heartbeat_window=heartbeat_window,
                )
        return current

    def _require(self, plan_execution_id: PlanExecutionId) -> PlanExecution:
        current = self.read(plan_execution_id)
        if current is None:
            raise InternalInvariantViolationError(
                "plan execution projection is missing",
                details={"plan_execution_id": str(plan_execution_id)},
            )
        return current

    def _record(
        self,
        current: PlanExecution,
        link: MilestoneRunLink,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> PlanExecution:
        updated = current.with_milestone(link)
        epoch = self._runtime.claim_epoch(now=now, heartbeat_window=heartbeat_window)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"plan-execution-update:{current.id}:{link.milestone_id}",
                events=(
                    EventWrite(
                        aggregate_type=PLAN_EXECUTION_AGGREGATE_TYPE,
                        aggregate_id=str(current.id),
                        expected_version=current.aggregate_version,
                        event_type="plan_execution.milestone_updated",
                        schema_version=1,
                        payload=plan_execution_to_dict(updated),
                    ),
                ),
                process_manager_updates=(
                    self._runtime.coordinator.epoch_guard(epoch=epoch.epoch, now=now),
                ),
            )
        )
        return updated


def _dependencies_succeeded(
    plan: Plan, milestone_id: PlanMilestoneId, current: PlanExecution
) -> bool:
    milestone = plan.milestone(milestone_id)
    return all(
        current.link(dependency).state is MilestoneRunState.SUCCEEDED
        for dependency in milestone.dependencies
    )


__all__ = ["PLAN_EXECUTION_AGGREGATE_TYPE", "PlanExecutionCoordinator", "derive_child_run_id"]
