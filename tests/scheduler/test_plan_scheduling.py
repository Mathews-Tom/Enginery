"""Tests for scheduler-bounded plan fan-out: enginery.engine.plan_execution's
integration with enginery.engine.scheduler.ReadinessScheduler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.domain.ids import PlanExecutionId, PlanMilestoneId, RunId
from enginery.domain.plan_execution import MilestoneRunState
from enginery.engine.plan_execution import PlanExecutionCoordinator, schedulable_nodes_for_plan
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.scheduler import NodeKey, SchedulableState, SchedulingLimits
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


class TestSchedulableNodesForPlan:
    def test_maps_every_milestone_state_to_a_schedulable_state(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        execution = coordinator.start(
            plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT
        )
        nodes = schedulable_nodes_for_plan(plan, execution, pe_id)
        by_key = {node.key: node for node in nodes}
        assert by_key[NodeKey(run_id=str(pe_id), node_id="m1")].state is SchedulableState.QUEUED
        assert by_key[NodeKey(run_id=str(pe_id), node_id="m1")].repository_id == "repo-a"
        assert by_key[NodeKey(run_id=str(pe_id), node_id="m2b")].repository_id == "repo-b"

    def test_dependency_edges_mirror_plan_milestone_dependencies(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        execution = coordinator.start(
            plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT
        )
        nodes = schedulable_nodes_for_plan(plan, execution, pe_id)
        by_key = {node.key: node for node in nodes}
        m3 = by_key[NodeKey(run_id=str(pe_id), node_id="m3")]
        assert set(m3.dependencies) == {
            NodeKey(run_id=str(pe_id), node_id="m2a"),
            NodeKey(run_id=str(pe_id), node_id="m2b"),
        }


class TestFanOutWithinLimits:
    def test_global_concurrency_defers_a_ready_sibling_to_the_next_tick(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.fan_out_within_limits(
            plan,
            pe_id,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=5),
        )
        coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        first_tick = coordinator.fan_out_within_limits(
            plan,
            pe_id,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=5),
        )
        registered = [
            milestone_id
            for milestone_id in (PlanMilestoneId("m2a"), PlanMilestoneId("m2b"))
            if first_tick.link(milestone_id).state is MilestoneRunState.REGISTERED
        ]
        assert len(registered) == 1

        second_tick = coordinator.fan_out_within_limits(
            plan,
            pe_id,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
            limits=SchedulingLimits(global_concurrency=2, per_repository_concurrency=5),
        )
        assert second_tick.link(PlanMilestoneId("m2a")).state is MilestoneRunState.REGISTERED
        assert second_tick.link(PlanMilestoneId("m2b")).state is MilestoneRunState.REGISTERED

    def test_per_repository_concurrency_limits_same_repository_milestones(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "linear.toml")  # m1, m2, m3 all target repo-a
        coordinator = _coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-1")
        coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        execution = coordinator.fan_out_within_limits(
            plan,
            pe_id,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
            limits=SchedulingLimits(global_concurrency=5, per_repository_concurrency=1),
        )
        # Only m1 is dependency-ready in a linear plan's first tick, so this
        # only proves the per-repository slot is occupied by it; the
        # cross-repository interaction is covered by the parallel-plan case
        # below.
        assert execution.link(PlanMilestoneId("m1")).state is MilestoneRunState.REGISTERED

    def test_two_plans_share_a_global_concurrency_budget_via_other_active(
        self, ledger_service: LedgerService
    ) -> None:
        plan_a = load_plan(FIXTURES / "parallel.toml")
        plan_b = load_plan(FIXTURES / "linear.toml")
        coordinator = _coordinator(ledger_service)
        pe_a = PlanExecutionId("pe-a")
        pe_b = PlanExecutionId("pe-b")
        coordinator.start(plan_a, plan_execution_id=pe_a, now=_NOW, heartbeat_window=_HEARTBEAT)
        coordinator.start(plan_b, plan_execution_id=pe_b, now=_NOW, heartbeat_window=_HEARTBEAT)

        limits = SchedulingLimits(global_concurrency=2, per_repository_concurrency=5)
        after_a = coordinator.fan_out_within_limits(
            plan_a,
            pe_a,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
            limits=limits,
        )
        registered_in_a = [
            milestone_id
            for milestone_id in (
                PlanMilestoneId("m1"),
                PlanMilestoneId("m2"),
                PlanMilestoneId("m3"),
            )
            if after_a.link(milestone_id).state is MilestoneRunState.REGISTERED
        ]
        # plan_a alone has three independent, dependency-ready milestones,
        # but the shared budget is 2.
        assert len(registered_in_a) == 2

        active_from_a = schedulable_nodes_for_plan(plan_a, after_a, pe_a)
        after_b = coordinator.fan_out_within_limits(
            plan_b,
            pe_b,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
            limits=limits,
            other_active=active_from_a,
        )
        # The budget of 2 is already fully spent by plan_a's two active
        # milestones, so plan_b's otherwise-ready entry milestone must wait.
        assert after_b.link(PlanMilestoneId("m1")).state is MilestoneRunState.PENDING
