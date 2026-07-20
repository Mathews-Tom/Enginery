"""``StackCoordinator``: durable, coordinator-fenced storage for one plan's ``Stack``.

Mirrors ``PlanExecutionCoordinator``'s read-then-conditionally-append
idempotent-creation pattern and its epoch-fenced update pattern, applied
to branch-topology evidence instead of child-run fan-out. A ``Stack``
mutation (publish, mark merge-ready, mark merged, reconcile after a
rebase) can touch several slices in one call -- ``reconcile_after_publish``
both publishes one slice and marks every later slice stale -- but is
always persisted as exactly one durable event: the caller-visible
``aggregate_version`` on the returned ``Stack`` always matches the
ledger's own tracked version for this aggregate, never a locally
incremented count of how many slices changed within one call.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta

from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, InternalInvariantViolationError
from enginery.domain.ids import PlanId, PlanMilestoneId, StackId
from enginery.domain.serialization import stack_from_dict, stack_to_dict
from enginery.domain.stack import Stack
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService

STACK_AGGREGATE_TYPE = "stack"


class StackCoordinator:
    """Coordinator-owned, crash-safe storage and reconciliation for one ``Stack``."""

    def __init__(self, ledger: LedgerService, runtime: CoordinatorRuntime) -> None:
        self._ledger = ledger
        self._runtime = runtime

    def read(self, stack_id: StackId) -> Stack | None:
        """Read the current durable projection.

        ``aggregate_version`` always comes from the ledger's own tracked
        version for this aggregate, never from the self-serialized
        payload field, matching ``PlanExecutionCoordinator.read``.
        """
        projection = self._ledger.read_projection(
            aggregate_type=STACK_AGGREGATE_TYPE, aggregate_id=str(stack_id)
        )
        if projection is None:
            return None
        stack = stack_from_dict(dict(projection.state))
        if stack.aggregate_version != projection.aggregate_version:
            stack = replace(stack, aggregate_version=projection.aggregate_version)
        return stack

    def start(
        self,
        *,
        stack_id: StackId,
        plan_id: PlanId,
        base_ref: str,
        ordered_milestones: Iterable[tuple[PlanMilestoneId, str]],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stack:
        """Idempotently create the durable topology record for one plan's stack."""
        materialized = tuple(ordered_milestones)
        existing = self.read(stack_id)
        initial = Stack.initial(
            stack_id=stack_id, plan_id=plan_id, base_ref=base_ref, ordered_milestones=materialized
        )
        if existing is not None:
            if (
                existing.plan_id != initial.plan_id
                or existing.base_ref != initial.base_ref
                or _topology_signature(existing) != _topology_signature(initial)
            ):
                raise ExternalConflictError(
                    "stack already exists with a different topology",
                    details={"stack_id": str(stack_id)},
                )
            return existing
        epoch = self._runtime.claim_epoch(now=now, heartbeat_window=heartbeat_window)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"stack-start:{stack_id}",
                events=(
                    EventWrite(
                        aggregate_type=STACK_AGGREGATE_TYPE,
                        aggregate_id=str(stack_id),
                        expected_version=0,
                        event_type="stack.started",
                        schema_version=1,
                        payload=stack_to_dict(initial),
                    ),
                ),
                process_manager_updates=(
                    self._runtime.coordinator.epoch_guard(epoch=epoch.epoch, now=now),
                ),
            )
        )
        return self._require(stack_id)

    def reconcile_after_publish(
        self,
        stack_id: StackId,
        milestone_id: PlanMilestoneId,
        *,
        head_revision: str,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stack:
        """Publish one slice's new head and mark every later slice stale, durably."""
        current = self._require(stack_id)
        updated = current.reconcile_after_publish(milestone_id, head_revision=head_revision)
        return self._persist(current, updated, now=now, heartbeat_window=heartbeat_window)

    def mark_merge_ready(
        self,
        stack_id: StackId,
        milestone_id: PlanMilestoneId,
        *,
        head_revision: str,
        ci_evidence_digest: Digest,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stack:
        """Record fresh, current-head CI evidence for one slice, durably."""
        current = self._require(stack_id)
        updated_slice = current.slice(milestone_id).mark_merge_ready(
            head_revision=head_revision, ci_evidence_digest=ci_evidence_digest
        )
        updated = current.with_slice(updated_slice)
        return self._persist(current, updated, now=now, heartbeat_window=heartbeat_window)

    def mark_merged(
        self,
        stack_id: StackId,
        milestone_id: PlanMilestoneId,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stack:
        """Record one slice as merged, durably. Enforces root-to-leaf order."""
        current = self._require(stack_id)
        updated = current.mark_merged(milestone_id)
        return self._persist(current, updated, now=now, heartbeat_window=heartbeat_window)

    def _require(self, stack_id: StackId) -> Stack:
        current = self.read(stack_id)
        if current is None:
            raise InternalInvariantViolationError(
                "stack projection is missing", details={"stack_id": str(stack_id)}
            )
        return current

    def _persist(
        self, current: Stack, updated: Stack, *, now: datetime, heartbeat_window: timedelta
    ) -> Stack:
        """Persist ``updated`` as exactly one durable event above ``current``.

        ``updated`` may have accumulated several local ``aggregate_version``
        increments if the domain-level call that produced it touched
        multiple slices (for example ``reconcile_after_publish``, which
        both publishes one slice and marks later slices stale). Regardless
        of how many, this method writes exactly one ledger event, so the
        returned ``Stack``'s ``aggregate_version`` is always exactly
        ``current.aggregate_version + 1`` -- the true post-append version
        -- never a stale locally accumulated count.
        """
        persisted = replace(updated, aggregate_version=current.aggregate_version + 1)
        epoch = self._runtime.claim_epoch(now=now, heartbeat_window=heartbeat_window)
        self._ledger.append(
            AppendCommand(
                correlation_id=f"stack-update:{current.id}",
                events=(
                    EventWrite(
                        aggregate_type=STACK_AGGREGATE_TYPE,
                        aggregate_id=str(current.id),
                        expected_version=current.aggregate_version,
                        event_type="stack.updated",
                        schema_version=1,
                        payload=stack_to_dict(persisted),
                    ),
                ),
                process_manager_updates=(
                    self._runtime.coordinator.epoch_guard(epoch=epoch.epoch, now=now),
                ),
            )
        )
        return persisted


def _topology_signature(stack: Stack) -> tuple[tuple[str, str, str], ...]:
    """A comparison key over immutable topology fields only (id, base,
    branch), ignoring mutable lifecycle state -- used to detect a
    conflicting re-start rather than a legitimate idempotent re-start."""
    return tuple(
        (str(slice_.milestone_id), slice_.base_ref, slice_.branch_ref)
        for slice_ in stack.ordered_slices
    )


__all__ = ["STACK_AGGREGATE_TYPE", "StackCoordinator"]
