"""Tests for enginery.engine.stack_coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.ids import PlanId, PlanMilestoneId, StackId
from enginery.domain.stack import StackSliceState
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=30)
_MILESTONES = (
    (PlanMilestoneId("m1"), "feature/m1"),
    (PlanMilestoneId("m2"), "feature/m2"),
    (PlanMilestoneId("m3"), "feature/m3"),
)


def _coordinator(ledger: LedgerService) -> StackCoordinator:
    return StackCoordinator(ledger, CoordinatorRuntime(ledger, owner="test-coordinator"))


class TestStart:
    def test_creates_a_pending_slice_for_every_milestone(
        self, ledger_service: LedgerService
    ) -> None:
        coordinator = _coordinator(ledger_service)
        stack = coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        assert set(stack.slices) == {
            PlanMilestoneId("m1"),
            PlanMilestoneId("m2"),
            PlanMilestoneId("m3"),
        }
        assert all(slice_.state is StackSliceState.PENDING for slice_ in stack.slices.values())
        assert stack.slice(PlanMilestoneId("m1")).base_ref == "main"
        assert stack.slice(PlanMilestoneId("m2")).base_ref == "feature/m1"

    def test_is_idempotent_across_repeated_calls(self, ledger_service: LedgerService) -> None:
        coordinator = _coordinator(ledger_service)
        first = coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        second = coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW + timedelta(seconds=5),
            heartbeat_window=_HEARTBEAT,
        )
        assert first == second

    def test_is_idempotent_across_a_simulated_coordinator_restart(
        self, ledger_service: LedgerService
    ) -> None:
        first_coordinator = _coordinator(ledger_service)
        before = first_coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        second_coordinator = _coordinator(ledger_service)
        after = second_coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW + timedelta(seconds=60),
            heartbeat_window=_HEARTBEAT,
        )
        assert before == after

    def test_rejects_reuse_of_the_same_id_for_a_different_topology(
        self, ledger_service: LedgerService
    ) -> None:
        coordinator = _coordinator(ledger_service)
        coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        with pytest.raises(ExternalConflictError, match="different topology"):
            coordinator.start(
                stack_id=StackId("stack-1"),
                plan_id=PlanId("plan-1"),
                base_ref="develop",
                ordered_milestones=_MILESTONES,
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )


class TestReconcileAfterPublish:
    def test_persists_across_multiple_slice_mutations_as_one_version_bump(
        self, ledger_service: LedgerService
    ) -> None:
        coordinator = _coordinator(ledger_service)
        stack_id = StackId("stack-1")
        coordinator.start(
            stack_id=stack_id,
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        before = coordinator.read(stack_id)
        assert before is not None
        for milestone_id, _ in _MILESTONES:
            coordinator.reconcile_after_publish(
                stack_id,
                milestone_id,
                head_revision=f"{milestone_id}-rev1",
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )
        # Republishing m1 both publishes m1 and marks m2 and m3 stale in
        # one domain-level call -- three slice mutations -- but must still
        # cost exactly one ledger version.
        before_republish = coordinator.read(stack_id)
        assert before_republish is not None
        after = coordinator.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev2",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        assert after.aggregate_version == before_republish.aggregate_version + 1
        assert after.slice(PlanMilestoneId("m2")).state is StackSliceState.STALE
        assert after.slice(PlanMilestoneId("m3")).state is StackSliceState.STALE
        # The durable read matches exactly what reconcile_after_publish
        # returned -- no drift between the returned value and storage.
        reread = coordinator.read(stack_id)
        assert reread == after


class TestMarkMergeReady:
    def test_records_ci_evidence_durably(self, ledger_service: LedgerService) -> None:
        coordinator = _coordinator(ledger_service)
        stack_id = StackId("stack-1")
        coordinator.start(
            stack_id=stack_id,
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        coordinator.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        ready = coordinator.mark_merge_ready(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev1",
            ci_evidence_digest=Digest.of_bytes(b"ci-passed"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        assert ready.slice(PlanMilestoneId("m1")).state is StackSliceState.MERGE_READY
        assert coordinator.read(stack_id) == ready

    def test_rejects_evidence_for_a_stale_head(self, ledger_service: LedgerService) -> None:
        coordinator = _coordinator(ledger_service)
        stack_id = StackId("stack-1")
        coordinator.start(
            stack_id=stack_id,
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        coordinator.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        with pytest.raises(InvalidInputError, match="does not match"):
            coordinator.mark_merge_ready(
                stack_id,
                PlanMilestoneId("m1"),
                head_revision="a-stale-sha-from-before-a-force-push",
                ci_evidence_digest=Digest.of_bytes(b"ci-passed"),
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )


class TestMarkMerged:
    def test_enforces_root_to_leaf_order(self, ledger_service: LedgerService) -> None:
        coordinator = _coordinator(ledger_service)
        stack_id = StackId("stack-1")
        coordinator.start(
            stack_id=stack_id,
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=_MILESTONES,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        for milestone_id, _ in _MILESTONES:
            coordinator.reconcile_after_publish(
                stack_id,
                milestone_id,
                head_revision=f"{milestone_id}-rev1",
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )
        coordinator.mark_merge_ready(
            stack_id,
            PlanMilestoneId("m2"),
            head_revision="m2-rev1",
            ci_evidence_digest=Digest.of_bytes(b"m2"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        with pytest.raises(InvalidInputError, match="before an earlier slice"):
            coordinator.mark_merged(
                stack_id, PlanMilestoneId("m2"), now=_NOW, heartbeat_window=_HEARTBEAT
            )

    def test_merging_root_then_leaf_succeeds_and_preserves_sibling_evidence(
        self, ledger_service: LedgerService
    ) -> None:
        coordinator = _coordinator(ledger_service)
        stack_id = StackId("stack-1")
        coordinator.start(
            stack_id=stack_id,
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=(_MILESTONES[0], _MILESTONES[1]),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        coordinator.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        coordinator.mark_merge_ready(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev1",
            ci_evidence_digest=Digest.of_bytes(b"m1"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        merged = coordinator.mark_merged(
            stack_id, PlanMilestoneId("m1"), now=_NOW, heartbeat_window=_HEARTBEAT
        )
        assert merged.slice(PlanMilestoneId("m1")).state is StackSliceState.MERGED
        assert merged.slice(PlanMilestoneId("m2")).state is StackSliceState.PENDING
        assert merged.next_mergeable() is None
