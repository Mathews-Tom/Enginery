"""Tests for enginery.domain.stack."""

from __future__ import annotations

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PlanId, PlanMilestoneId, StackId
from enginery.domain.serialization import stack_from_dict, stack_to_dict
from enginery.domain.stack import (
    STACK_SLICE_TRANSITIONS,
    Stack,
    StackSlice,
    StackSliceState,
)
from tests.domain.test_state_machine import TestEveryDomainTransitionTableHasNoDeadEnds


def _stack(milestones: tuple[str, ...] = ("m1", "m2", "m3")) -> Stack:
    return Stack.initial(
        stack_id=StackId("stack-1"),
        plan_id=PlanId("plan-1"),
        base_ref="main",
        ordered_milestones=[(PlanMilestoneId(value), f"feature/{value}") for value in milestones],
    )


class TestStackSliceState:
    def test_has_the_six_designed_states(self) -> None:
        assert {member.value for member in StackSliceState} == {
            "pending",
            "published",
            "merge_ready",
            "stale",
            "merged",
            "abandoned",
        }

    def test_has_no_dead_ends(self) -> None:
        TestEveryDomainTransitionTableHasNoDeadEnds.assert_every_non_terminal_state_reaches_a_terminal(
            STACK_SLICE_TRANSITIONS
        )


class TestStackSlice:
    def test_pending_slice_has_no_head_revision(self) -> None:
        slice_ = StackSlice(
            milestone_id=PlanMilestoneId("m1"), position=1, base_ref="main", branch_ref="feature/m1"
        )
        assert slice_.head_revision is None

    def test_position_must_be_at_least_one(self) -> None:
        with pytest.raises(InvalidInputError, match="at least 1"):
            StackSlice(
                milestone_id=PlanMilestoneId("m1"),
                position=0,
                base_ref="main",
                branch_ref="feature/m1",
            )

    def test_publish_binds_a_head_revision(self) -> None:
        slice_ = StackSlice(
            milestone_id=PlanMilestoneId("m1"), position=1, base_ref="main", branch_ref="feature/m1"
        )
        published = slice_.publish(head_revision="abc123")
        assert published.state is StackSliceState.PUBLISHED
        assert published.head_revision == "abc123"

    def test_mark_merge_ready_requires_matching_head(self) -> None:
        published = StackSlice(
            milestone_id=PlanMilestoneId("m1"),
            position=1,
            base_ref="main",
            branch_ref="feature/m1",
            state=StackSliceState.PUBLISHED,
            head_revision="abc123",
        )
        with pytest.raises(InvalidInputError, match="does not match"):
            published.mark_merge_ready(
                head_revision="def456", ci_evidence_digest=Digest.of_bytes(b"ci")
            )

    def test_mark_merge_ready_with_matching_head_succeeds(self) -> None:
        published = StackSlice(
            milestone_id=PlanMilestoneId("m1"),
            position=1,
            base_ref="main",
            branch_ref="feature/m1",
            state=StackSliceState.PUBLISHED,
            head_revision="abc123",
        )
        ready = published.mark_merge_ready(
            head_revision="abc123", ci_evidence_digest=Digest.of_bytes(b"ci")
        )
        assert ready.state is StackSliceState.MERGE_READY
        assert ready.ci_evidence_digest == Digest.of_bytes(b"ci")

    def test_merge_ready_slice_requires_ci_evidence(self) -> None:
        with pytest.raises(InvalidInputError, match="requires bound CI evidence"):
            StackSlice(
                milestone_id=PlanMilestoneId("m1"),
                position=1,
                base_ref="main",
                branch_ref="feature/m1",
                state=StackSliceState.MERGE_READY,
                head_revision="abc123",
            )

    def test_mark_stale_clears_ci_evidence(self) -> None:
        ready = StackSlice(
            milestone_id=PlanMilestoneId("m1"),
            position=1,
            base_ref="main",
            branch_ref="feature/m1",
            state=StackSliceState.MERGE_READY,
            head_revision="abc123",
            ci_evidence_digest=Digest.of_bytes(b"ci"),
        )
        stale = ready.mark_stale()
        assert stale.state is StackSliceState.STALE
        assert stale.ci_evidence_digest is None
        assert stale.head_revision == "abc123"

    def test_mark_stale_on_a_pending_slice_is_a_no_op(self) -> None:
        pending = StackSlice(
            milestone_id=PlanMilestoneId("m1"), position=1, base_ref="main", branch_ref="feature/m1"
        )
        assert pending.mark_stale() == pending

    def test_republish_from_stale_requires_a_fresh_head(self) -> None:
        stale = StackSlice(
            milestone_id=PlanMilestoneId("m1"),
            position=1,
            base_ref="main",
            branch_ref="feature/m1",
            state=StackSliceState.STALE,
            head_revision="abc123",
        )
        republished = stale.publish(head_revision="def456")
        assert republished.state is StackSliceState.PUBLISHED
        assert republished.head_revision == "def456"

    def test_illegal_transition_is_rejected(self) -> None:
        pending = StackSlice(
            milestone_id=PlanMilestoneId("m1"), position=1, base_ref="main", branch_ref="feature/m1"
        )
        with pytest.raises(InvalidInputError, match="illegal transition"):
            pending.mark_merged()


class TestStackConstruction:
    def test_requires_at_least_one_slice(self) -> None:
        with pytest.raises(InvalidInputError, match="at least one slice"):
            Stack(id=StackId("s-1"), plan_id=PlanId("p-1"), base_ref="main", slices={})

    def test_positions_must_be_exactly_one_to_n_with_no_gaps(self) -> None:
        with pytest.raises(InvalidInputError, match=r"1\.\.N"):
            Stack(
                id=StackId("s-1"),
                plan_id=PlanId("p-1"),
                base_ref="main",
                slices={
                    PlanMilestoneId("m1"): StackSlice(
                        milestone_id=PlanMilestoneId("m1"),
                        position=1,
                        base_ref="main",
                        branch_ref="feature/m1",
                    ),
                    PlanMilestoneId("m2"): StackSlice(
                        milestone_id=PlanMilestoneId("m2"),
                        position=3,
                        base_ref="feature/m1",
                        branch_ref="feature/m2",
                    ),
                },
            )

    def test_initial_seeds_base_ref_chain(self) -> None:
        stack = _stack()
        assert stack.slice(PlanMilestoneId("m1")).base_ref == "main"
        assert stack.slice(PlanMilestoneId("m2")).base_ref == "feature/m1"
        assert stack.slice(PlanMilestoneId("m3")).base_ref == "feature/m2"


class TestReconcileAfterPublish:
    def test_publishing_a_lower_slice_marks_later_slices_stale(self) -> None:
        stack = _stack()
        # Publish and mark every slice merge_ready first, as if the stack
        # had already passed CI once end to end.
        for milestone in ("m1", "m2", "m3"):
            head = f"{milestone}-rev1"
            stack = stack.reconcile_after_publish(PlanMilestoneId(milestone), head_revision=head)
            stack = stack.with_slice(
                stack.slice(PlanMilestoneId(milestone)).mark_merge_ready(
                    head_revision=head, ci_evidence_digest=Digest.of_bytes(head.encode())
                )
            )
        assert all(slice_.state is StackSliceState.MERGE_READY for slice_ in stack.ordered_slices)

        # A rebase changes m1's head; m2 and m3 must be invalidated.
        rebased = stack.reconcile_after_publish(PlanMilestoneId("m1"), head_revision="m1-rev2")
        assert rebased.slice(PlanMilestoneId("m1")).state is StackSliceState.PUBLISHED
        assert rebased.slice(PlanMilestoneId("m2")).state is StackSliceState.STALE
        assert rebased.slice(PlanMilestoneId("m3")).state is StackSliceState.STALE

    def test_publishing_a_never_before_published_pending_slice_leaves_later_pending_slices_alone(
        self,
    ) -> None:
        stack = _stack()
        published = stack.reconcile_after_publish(PlanMilestoneId("m1"), head_revision="m1-rev1")
        assert published.slice(PlanMilestoneId("m2")).state is StackSliceState.PENDING
        assert published.slice(PlanMilestoneId("m3")).state is StackSliceState.PENDING


class TestNextMergeable:
    def test_none_when_no_slice_is_merge_ready(self) -> None:
        assert _stack().next_mergeable() is None

    def test_root_slice_when_it_is_merge_ready(self) -> None:
        stack = _stack()
        stack = stack.reconcile_after_publish(PlanMilestoneId("m1"), head_revision="m1-rev1")
        stack = stack.with_slice(
            stack.slice(PlanMilestoneId("m1")).mark_merge_ready(
                head_revision="m1-rev1", ci_evidence_digest=Digest.of_bytes(b"m1")
            )
        )
        assert stack.next_mergeable() == PlanMilestoneId("m1")

    def test_none_when_the_next_position_is_still_pending_even_if_a_later_one_is_ready(
        self,
    ) -> None:
        # This cannot happen through reconcile_after_publish's own chain
        # (m2 can only become MERGE_READY after being published, and
        # publishing m2 requires m1 to already exist), but next_mergeable
        # must still refuse to skip an unmerged earlier position even if
        # constructed directly, since root-to-leaf order is a hard
        # invariant, not merely the common case.
        stack = _stack()
        m2_ready = StackSlice(
            milestone_id=PlanMilestoneId("m2"),
            position=2,
            base_ref="feature/m1",
            branch_ref="feature/m2",
            state=StackSliceState.MERGE_READY,
            head_revision="m2-rev1",
            ci_evidence_digest=Digest.of_bytes(b"m2"),
        )
        stack = stack.with_slice(m2_ready)
        assert stack.next_mergeable() is None


class TestMarkMerged:
    def test_merging_out_of_order_is_rejected(self) -> None:
        stack = _stack()
        # m2 was never actually published in isolation here (m1 must exist
        # first in a real git ancestry), but domain-level construction lets
        # us prove the ordering guard directly regardless of git state.
        stack = stack.with_slice(
            StackSlice(
                milestone_id=PlanMilestoneId("m2"),
                position=2,
                base_ref="feature/m1",
                branch_ref="feature/m2",
                state=StackSliceState.MERGE_READY,
                head_revision="m2-rev1",
                ci_evidence_digest=Digest.of_bytes(b"m2"),
            )
        )
        with pytest.raises(InvalidInputError, match="before an earlier slice"):
            stack.mark_merged(PlanMilestoneId("m2"))

    def test_merging_in_order_succeeds_and_preserves_later_slice_state(self) -> None:
        stack = _stack(("m1", "m2"))
        stack = stack.reconcile_after_publish(PlanMilestoneId("m1"), head_revision="m1-rev1")
        stack = stack.with_slice(
            stack.slice(PlanMilestoneId("m1")).mark_merge_ready(
                head_revision="m1-rev1", ci_evidence_digest=Digest.of_bytes(b"m1")
            )
        )
        merged = stack.mark_merged(PlanMilestoneId("m1"))
        assert merged.slice(PlanMilestoneId("m1")).state is StackSliceState.MERGED
        assert merged.slice(PlanMilestoneId("m2")).state is StackSliceState.PENDING


class TestStackRoundTrip:
    def test_round_trips_through_mapping(self) -> None:
        stack = _stack()
        stack = stack.reconcile_after_publish(PlanMilestoneId("m1"), head_revision="m1-rev1")
        rebuilt = Stack.from_mapping(stack.to_mapping())
        assert rebuilt == stack

    def test_envelope_round_trips(self) -> None:
        stack = _stack()
        envelope = stack_to_dict(stack)
        assert envelope["schema_version"] == 1
        assert stack_from_dict(envelope) == stack

    def test_envelope_rejects_a_mismatched_schema_version(self) -> None:
        stack = _stack()
        envelope = stack_to_dict(stack)
        envelope["schema_version"] = 999
        with pytest.raises(InvalidInputError, match="schema_version"):
            stack_from_dict(envelope)
