"""Tests for enginery.domain.plan_execution."""

from __future__ import annotations

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PlanExecutionId, PlanId, PlanMilestoneId, RunId
from enginery.domain.plan_execution import (
    MILESTONE_RUN_TRANSITIONS,
    MilestoneRunLink,
    MilestoneRunState,
    PlanExecution,
    PlanExecutionState,
)
from enginery.domain.serialization import plan_execution_from_dict, plan_execution_to_dict
from tests.domain.test_state_machine import TestEveryDomainTransitionTableHasNoDeadEnds


def _plan_execution(milestone_ids: tuple[str, ...] = ("m1", "m2")) -> PlanExecution:
    return PlanExecution.initial(
        plan_execution_id=PlanExecutionId("pe-1"),
        plan_id=PlanId("plan-1"),
        plan_digest=Digest.of_bytes(b"plan content"),
        milestone_ids=tuple(PlanMilestoneId(value) for value in milestone_ids),
    )


class TestMilestoneRunState:
    def test_has_the_six_designed_states(self) -> None:
        assert {member.value for member in MilestoneRunState} == {
            "pending",
            "registered",
            "succeeded",
            "blocked",
            "failed",
            "cancelled",
        }

    def test_has_no_dead_ends(self) -> None:
        TestEveryDomainTransitionTableHasNoDeadEnds.assert_every_non_terminal_state_reaches_a_terminal(
            MILESTONE_RUN_TRANSITIONS
        )


class TestMilestoneRunLink:
    def test_pending_link_has_no_bound_run(self) -> None:
        link = MilestoneRunLink(milestone_id=PlanMilestoneId("m1"), state=MilestoneRunState.PENDING)
        assert link.run_id is None

    def test_registered_link_requires_a_bound_run(self) -> None:
        with pytest.raises(InvalidInputError, match="requires a bound run_id"):
            MilestoneRunLink(milestone_id=PlanMilestoneId("m1"), state=MilestoneRunState.REGISTERED)

    def test_pending_link_rejects_a_bound_run(self) -> None:
        with pytest.raises(InvalidInputError, match="cannot already be bound"):
            MilestoneRunLink(
                milestone_id=PlanMilestoneId("m1"),
                state=MilestoneRunState.PENDING,
                run_id=RunId("run-1"),
            )

    def test_transition_to_registered_binds_the_run(self) -> None:
        link = MilestoneRunLink(milestone_id=PlanMilestoneId("m1"), state=MilestoneRunState.PENDING)
        registered = link.transition_to(MilestoneRunState.REGISTERED, run_id=RunId("run-1"))
        assert registered.state is MilestoneRunState.REGISTERED
        assert registered.run_id == RunId("run-1")

    def test_transition_to_succeeded_retains_the_bound_run(self) -> None:
        link = MilestoneRunLink(
            milestone_id=PlanMilestoneId("m1"),
            state=MilestoneRunState.REGISTERED,
            run_id=RunId("run-1"),
        )
        succeeded = link.transition_to(MilestoneRunState.SUCCEEDED)
        assert succeeded.run_id == RunId("run-1")

    def test_illegal_transition_is_rejected(self) -> None:
        link = MilestoneRunLink(milestone_id=PlanMilestoneId("m1"), state=MilestoneRunState.PENDING)
        with pytest.raises(InvalidInputError, match="illegal transition"):
            link.transition_to(MilestoneRunState.SUCCEEDED)

    def test_terminal_link_rejects_any_further_transition(self) -> None:
        link = MilestoneRunLink(
            milestone_id=PlanMilestoneId("m1"),
            state=MilestoneRunState.SUCCEEDED,
            run_id=RunId("run-1"),
        )
        with pytest.raises(InvalidInputError, match="illegal transition"):
            link.transition_to(MilestoneRunState.FAILED)


class TestPlanExecutionState:
    def test_initial_state_is_running(self) -> None:
        assert _plan_execution().state is PlanExecutionState.RUNNING

    def test_all_succeeded_milestones_yield_succeeded_plan(self) -> None:
        execution = _plan_execution(("m1",))
        registered = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-1")
            )
        )
        succeeded = registered.with_milestone(
            registered.link(PlanMilestoneId("m1")).transition_to(MilestoneRunState.SUCCEEDED)
        )
        assert succeeded.state is PlanExecutionState.SUCCEEDED

    def test_one_failed_milestone_yields_failed_plan_but_preserves_succeeded_sibling(self) -> None:
        execution = _plan_execution(("m1", "m2"))
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-1")
            )
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(MilestoneRunState.SUCCEEDED)
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m2")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-2")
            )
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m2")).transition_to(MilestoneRunState.FAILED)
        )
        assert execution.state is PlanExecutionState.FAILED
        assert execution.link(PlanMilestoneId("m1")).state is MilestoneRunState.SUCCEEDED
        assert execution.link(PlanMilestoneId("m1")).run_id == RunId("run-1")

    def test_one_blocked_milestone_yields_blocked_plan_when_no_milestone_failed(self) -> None:
        execution = _plan_execution(("m1",))
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-1")
            )
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(MilestoneRunState.BLOCKED)
        )
        assert execution.state is PlanExecutionState.BLOCKED

    def test_failed_takes_precedence_over_blocked(self) -> None:
        execution = _plan_execution(("m1", "m2"))
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-1")
            )
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(MilestoneRunState.BLOCKED)
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m2")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-2")
            )
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m2")).transition_to(MilestoneRunState.FAILED)
        )
        assert execution.state is PlanExecutionState.FAILED

    def test_a_cancelled_milestone_with_the_rest_succeeded_yields_cancelled_plan(self) -> None:
        execution = _plan_execution(("m1", "m2"))
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(MilestoneRunState.CANCELLED)
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m2")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-2")
            )
        )
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m2")).transition_to(MilestoneRunState.SUCCEEDED)
        )
        assert execution.state is PlanExecutionState.CANCELLED


class TestPlanExecutionRoundTrip:
    def test_round_trips_through_mapping(self) -> None:
        execution = _plan_execution(("m1", "m2"))
        execution = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-1")
            )
        )
        rebuilt = PlanExecution.from_mapping(execution.to_mapping())
        assert rebuilt == execution

    def test_requires_at_least_one_milestone(self) -> None:
        with pytest.raises(InvalidInputError, match="at least one milestone"):
            PlanExecution(
                id=PlanExecutionId("pe-1"),
                plan_id=PlanId("plan-1"),
                plan_digest=Digest.of_bytes(b"x"),
                milestones={},
            )

    def test_with_milestone_rejects_an_untracked_milestone(self) -> None:
        execution = _plan_execution(("m1",))
        with pytest.raises(InvalidInputError, match="does not track this milestone"):
            execution.with_milestone(
                MilestoneRunLink(
                    milestone_id=PlanMilestoneId("m2"), state=MilestoneRunState.PENDING
                )
            )

    def test_with_milestone_increments_aggregate_version(self) -> None:
        execution = _plan_execution(("m1",))
        updated = execution.with_milestone(
            execution.link(PlanMilestoneId("m1")).transition_to(
                MilestoneRunState.REGISTERED, run_id=RunId("run-1")
            )
        )
        assert updated.aggregate_version == execution.aggregate_version + 1


class TestPlanExecutionEnvelope:
    def test_envelope_round_trips(self) -> None:
        execution = _plan_execution(("m1",))
        envelope = plan_execution_to_dict(execution)
        assert envelope["schema_version"] == 1
        assert plan_execution_from_dict(envelope) == execution

    def test_envelope_rejects_a_mismatched_schema_version(self) -> None:
        execution = _plan_execution(("m1",))
        envelope = plan_execution_to_dict(execution)
        envelope["schema_version"] = 999
        with pytest.raises(InvalidInputError, match="schema_version"):
            plan_execution_from_dict(envelope)
