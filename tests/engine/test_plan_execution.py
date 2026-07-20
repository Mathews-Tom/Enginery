"""Tests for enginery.engine.plan_execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.domain.ids import PlanExecutionId, PlanMilestoneId, RunId
from enginery.domain.plan_execution import MilestoneRunState, PlanExecutionState
from enginery.engine.plan_execution import PlanExecutionCoordinator, derive_child_run_id
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.service import LedgerService
from enginery.plans.loader import load_plan
from enginery.plans.model import Plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plans"
_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=30)


def _child_run_state(plan: Plan, milestone_id: PlanMilestoneId, run_id: RunId) -> dict[str, object]:
    return {"run_id": str(run_id), "plan_id": str(plan.id), "milestone_id": str(milestone_id)}


def _coordinator(ledger: LedgerService) -> PlanExecutionCoordinator:
    return PlanExecutionCoordinator(ledger, CoordinatorRuntime(ledger, owner="test-coordinator"))


class TestDeriveChildRunId:
    def test_is_deterministic_for_the_same_plan_and_milestone(self) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        first = derive_child_run_id(plan, PlanMilestoneId("m1"))
        second = derive_child_run_id(plan, PlanMilestoneId("m1"))
        assert first == second

    def test_differs_across_milestones(self) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        assert derive_child_run_id(plan, PlanMilestoneId("m1")) != derive_child_run_id(
            plan, PlanMilestoneId("m2")
        )

    def test_differs_across_plans(self) -> None:
        linear = load_plan(FIXTURES / "linear.toml")
        parallel = load_plan(FIXTURES / "parallel.toml")
        assert derive_child_run_id(linear, PlanMilestoneId("m1")) != derive_child_run_id(
            parallel, PlanMilestoneId("m1")
        )


class TestStart:
    def test_creates_a_pending_link_for_every_milestone(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        coordinator = _coordinator(ledger_service)
        execution = coordinator.start(
            plan, plan_execution_id=PlanExecutionId("pe-1"), now=_NOW, heartbeat_window=_HEARTBEAT
        )
        assert set(execution.milestones) == {
            PlanMilestoneId("m1"),
            PlanMilestoneId("m2a"),
            PlanMilestoneId("m2b"),
            PlanMilestoneId("m3"),
        }
        assert all(
            link.state is MilestoneRunState.PENDING for link in execution.milestones.values()
        )

    def test_is_idempotent_across_repeated_calls(self, ledger_service: LedgerService) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        coordinator = _coordinator(ledger_service)
        first = coordinator.start(
            plan, plan_execution_id=PlanExecutionId("pe-1"), now=_NOW, heartbeat_window=_HEARTBEAT
        )
        second = coordinator.start(
            plan,
            plan_execution_id=PlanExecutionId("pe-1"),
            now=_NOW + timedelta(seconds=5),
            heartbeat_window=_HEARTBEAT,
        )
        assert first == second

    def test_rejects_reuse_of_the_same_id_for_a_different_plan(
        self, ledger_service: LedgerService
    ) -> None:
        coordinator = _coordinator(ledger_service)
        coordinator.start(
            load_plan(FIXTURES / "linear.toml"),
            plan_execution_id=PlanExecutionId("pe-1"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        with pytest.raises(ExternalConflictError, match="already exists for a different plan"):
            coordinator.start(
                load_plan(FIXTURES / "parallel.toml"),
                plan_execution_id=PlanExecutionId("pe-1"),
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )


class TestFanOut:
    def test_linear_plan_registers_only_the_entry_milestone_first(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        coordinator = _coordinator(ledger_service)
        coordinator.start(
            plan, plan_execution_id=PlanExecutionId("pe-1"), now=_NOW, heartbeat_window=_HEARTBEAT
        )
        execution = coordinator.fan_out(
            plan,
            PlanExecutionId("pe-1"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
        )
        assert execution.link(PlanMilestoneId("m1")).state is MilestoneRunState.REGISTERED
        assert execution.link(PlanMilestoneId("m2")).state is MilestoneRunState.PENDING
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.PENDING

    def test_parallel_plan_registers_every_independent_milestone_in_one_call(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "parallel.toml")
        coordinator = _coordinator(ledger_service)
        coordinator.start(
            plan, plan_execution_id=PlanExecutionId("pe-1"), now=_NOW, heartbeat_window=_HEARTBEAT
        )
        execution = coordinator.fan_out(
            plan,
            PlanExecutionId("pe-1"),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
        )
        assert all(
            link.state is MilestoneRunState.REGISTERED for link in execution.milestones.values()
        )
        run_ids = {link.run_id for link in execution.milestones.values()}
        assert len(run_ids) == 3

    def test_diamond_join_milestone_is_not_registered_until_both_branches_succeed(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        execution = coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert execution.link(PlanMilestoneId("m2a")).state is MilestoneRunState.REGISTERED
        assert execution.link(PlanMilestoneId("m2b")).state is MilestoneRunState.REGISTERED
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.PENDING

        coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m2a"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        still_waiting = coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert still_waiting.link(PlanMilestoneId("m3")).state is MilestoneRunState.PENDING

        coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m2b"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        joined = coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert joined.link(PlanMilestoneId("m3")).state is MilestoneRunState.REGISTERED

    def test_fan_out_after_a_simulated_coordinator_crash_creates_no_duplicate_child_run(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "parallel.toml")
        pe_id = PlanExecutionId("pe-1")
        first_coordinator = _coordinator(ledger_service)
        first_coordinator.start(
            plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT
        )
        before_crash = first_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        run_ids_before = {
            milestone_id: link.run_id for milestone_id, link in before_crash.milestones.items()
        }

        # Simulate a coordinator restart: a brand new CoordinatorRuntime and
        # PlanExecutionCoordinator over the same durable ledger, exactly like
        # the recovery topology after a coordinator process is replaced.
        second_coordinator = _coordinator(ledger_service)
        after_recovery = second_coordinator.fan_out(
            plan,
            pe_id,
            now=_NOW + timedelta(seconds=45),
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
        )
        run_ids_after = {
            milestone_id: link.run_id for milestone_id, link in after_recovery.milestones.items()
        }
        assert run_ids_after == run_ids_before


class TestJoinPreservesCompletedSiblings:
    def test_one_failed_milestone_leaves_a_succeeded_siblings_run_id_intact(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "parallel.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        after_fan_out = coordinator.read(pe_id)
        assert after_fan_out is not None
        succeeded_run_id = after_fan_out.link(PlanMilestoneId("m1")).run_id
        coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        execution = coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m2"),
            MilestoneRunState.FAILED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        assert execution.state is PlanExecutionState.FAILED
        assert execution.link(PlanMilestoneId("m1")).state is MilestoneRunState.SUCCEEDED
        assert execution.link(PlanMilestoneId("m1")).run_id == succeeded_run_id
        # m3 was independent and never touched by m2's failure.
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.REGISTERED

    def test_recording_the_same_terminal_outcome_twice_is_a_no_op(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        first = coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        second = coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        assert first.aggregate_version == second.aggregate_version


class TestCancelUnregistered:
    def test_cancels_only_pending_milestones(self, ledger_service: LedgerService) -> None:
        plan = load_plan(FIXTURES / "parallel.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        # m1, m2, m3 are all independent and all registered already, so
        # re-derive a mixed scenario: cancel before any registration by
        # starting a fresh plan execution instead.
        pe_id_2 = PlanExecutionId("pe-2")
        coordinator.start(plan, plan_execution_id=pe_id_2, now=_NOW, heartbeat_window=_HEARTBEAT)
        cancelled = coordinator.cancel_unregistered(pe_id_2, now=_NOW, heartbeat_window=_HEARTBEAT)
        assert all(
            link.state is MilestoneRunState.CANCELLED for link in cancelled.milestones.values()
        )
        assert cancelled.state is PlanExecutionState.CANCELLED

    def test_registered_milestones_are_untouched_by_cancel_unregistered(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        cancelled = coordinator.cancel_unregistered(pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        assert cancelled.link(PlanMilestoneId("m1")).state is MilestoneRunState.REGISTERED
        assert cancelled.link(PlanMilestoneId("m2")).state is MilestoneRunState.CANCELLED
        assert cancelled.link(PlanMilestoneId("m3")).state is MilestoneRunState.CANCELLED
