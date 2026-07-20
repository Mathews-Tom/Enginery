"""End-to-end plan-to-child-run and stack-topology coverage.

Exercises the full pipeline (``load_plan`` -> ``PlanExecutionCoordinator``
fan-out/join -> ``StackCoordinator`` publish/merge-ready/merge) across
linear, parallel, diamond, failed, cancelled, and resumed plans, matching
the milestone's own linear/parallel/diamond/failed/cancelled/resumed
acceptance language.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.domain.digests import Digest
from enginery.domain.ids import PlanExecutionId, PlanMilestoneId, RunId, StackId
from enginery.domain.plan_execution import MilestoneRunState, PlanExecutionState
from enginery.domain.stack import StackSliceState
from enginery.engine.plan_execution import PlanExecutionCoordinator
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService
from enginery.plans.loader import load_plan
from enginery.plans.model import Plan

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plans"
_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=30)


def _child_run_state(plan: Plan, milestone_id: PlanMilestoneId, run_id: RunId) -> dict[str, object]:
    return {"run_id": str(run_id), "plan_id": str(plan.id), "milestone_id": str(milestone_id)}


def _plan_coordinator(ledger: LedgerService) -> PlanExecutionCoordinator:
    return PlanExecutionCoordinator(ledger, CoordinatorRuntime(ledger, owner="test-coordinator"))


def _stack_coordinator(ledger: LedgerService) -> StackCoordinator:
    return StackCoordinator(ledger, CoordinatorRuntime(ledger, owner="test-coordinator"))


def _publish_and_verify(
    stack_coordinator: StackCoordinator,
    stack_id: StackId,
    milestone_id: PlanMilestoneId,
    *,
    run_id: RunId,
) -> None:
    head = f"{milestone_id}-{run_id}"
    stack_coordinator.reconcile_after_publish(
        stack_id, milestone_id, head_revision=head, now=_NOW, heartbeat_window=_HEARTBEAT
    )
    stack_coordinator.mark_merge_ready(
        stack_id,
        milestone_id,
        head_revision=head,
        ci_evidence_digest=Digest.of_bytes(head.encode("utf-8")),
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )


def _seed_stack(stack_coordinator: StackCoordinator, plan: Plan, stack_id: StackId) -> None:
    stack_coordinator.start(
        stack_id=stack_id,
        plan_id=plan.id,
        base_ref="main",
        ordered_milestones=[
            (milestone_id, f"feature/{milestone_id}") for milestone_id in plan.topological_order()
        ],
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )


class TestLinearPlan:
    def test_completes_root_to_leaf_with_correct_child_and_branch_topology(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "linear.toml")
        plan_coordinator = _plan_coordinator(ledger_service)
        stack_coordinator = _stack_coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-linear")
        stack_id = StackId("stack-linear")

        plan_coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        _seed_stack(stack_coordinator, plan, stack_id)

        for milestone_id in plan.topological_order():
            execution = plan_coordinator.fan_out(
                plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
            )
            link = execution.link(milestone_id)
            assert link.state is MilestoneRunState.REGISTERED
            assert link.run_id is not None
            _publish_and_verify(stack_coordinator, stack_id, milestone_id, run_id=link.run_id)
            plan_coordinator.record_milestone_outcome(
                pe_id,
                milestone_id,
                MilestoneRunState.SUCCEEDED,
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )

        final_execution = plan_coordinator.read(pe_id)
        assert final_execution is not None
        assert final_execution.state is PlanExecutionState.SUCCEEDED
        run_ids = {link.run_id for link in final_execution.milestones.values()}
        assert len(run_ids) == 3  # no duplicate child run

        stack = stack_coordinator.read(stack_id)
        assert stack is not None
        for milestone_id in plan.topological_order():
            stack = stack_coordinator.mark_merged(
                stack_id, milestone_id, now=_NOW, heartbeat_window=_HEARTBEAT
            )
        assert all(slice_.state is StackSliceState.MERGED for slice_ in stack.slices.values())


class TestParallelPlan:
    def test_independent_milestones_all_register_without_duplication(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "parallel.toml")
        plan_coordinator = _plan_coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-parallel")
        plan_coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        run_ids = [link.run_id for link in execution.milestones.values()]
        assert all(run_id is not None for run_id in run_ids)
        assert len(set(run_ids)) == 3

        # A second fan_out call must not re-register any already-registered
        # milestone (no duplicate child run on a redundant call either).
        again = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert again == execution


class TestDiamondPlan:
    def test_join_milestone_waits_for_both_branches_with_correct_topology(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        plan_coordinator = _plan_coordinator(ledger_service)
        stack_coordinator = _stack_coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-diamond")
        stack_id = StackId("stack-diamond")
        plan_coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        _seed_stack(stack_coordinator, plan, stack_id)

        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert execution.link(PlanMilestoneId("m1")).state is MilestoneRunState.REGISTERED
        assert execution.link(PlanMilestoneId("m2a")).state is MilestoneRunState.PENDING
        assert execution.link(PlanMilestoneId("m2b")).state is MilestoneRunState.PENDING
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.PENDING

        m1_run_id = execution.link(PlanMilestoneId("m1")).run_id
        assert m1_run_id is not None
        _publish_and_verify(stack_coordinator, stack_id, PlanMilestoneId("m1"), run_id=m1_run_id)
        plan_coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )

        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert execution.link(PlanMilestoneId("m2a")).state is MilestoneRunState.REGISTERED
        assert execution.link(PlanMilestoneId("m2b")).state is MilestoneRunState.REGISTERED
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.PENDING

        for milestone_id in (PlanMilestoneId("m2a"), PlanMilestoneId("m2b")):
            run_id = execution.link(milestone_id).run_id
            assert run_id is not None
            _publish_and_verify(stack_coordinator, stack_id, milestone_id, run_id=run_id)
            plan_coordinator.record_milestone_outcome(
                pe_id,
                milestone_id,
                MilestoneRunState.SUCCEEDED,
                now=_NOW,
                heartbeat_window=_HEARTBEAT,
            )

        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.REGISTERED
        all_run_ids = [link.run_id for link in execution.milestones.values()]
        assert len(set(all_run_ids)) == 4  # no duplicate child run across the whole diamond


class TestFailedPlan:
    def test_a_failed_milestone_blocks_its_dependent_but_preserves_the_other_branch(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        plan_coordinator = _plan_coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-failed")
        plan_coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        plan_coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        m2a_run_id = execution.link(PlanMilestoneId("m2a")).run_id
        m2b_run_id = execution.link(PlanMilestoneId("m2b")).run_id
        assert m2a_run_id is not None and m2b_run_id is not None

        # m2a fails; m2b succeeds.
        plan_coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m2a"),
            MilestoneRunState.FAILED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        plan_coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m2b"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )

        assert execution.state is PlanExecutionState.FAILED
        # m3 depends on both m2a and m2b; m2a never succeeded, so m3 must
        # never be registered -- a failed dependency permanently blocks a
        # dependent's fan-out rather than merely delaying it.
        assert execution.link(PlanMilestoneId("m3")).state is MilestoneRunState.PENDING
        assert execution.link(PlanMilestoneId("m3")).run_id is None
        # The completed sibling's evidence is preserved, not erased.
        assert execution.link(PlanMilestoneId("m2b")).state is MilestoneRunState.SUCCEEDED
        assert execution.link(PlanMilestoneId("m2b")).run_id == m2b_run_id
        assert execution.link(PlanMilestoneId("m2a")).run_id == m2a_run_id


class TestCancelledPlan:
    def test_cancel_unregistered_leaves_a_running_sibling_untouched(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        plan_coordinator = _plan_coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-cancelled")
        plan_coordinator.start(plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT)
        execution = plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        m1_run_id = execution.link(PlanMilestoneId("m1")).run_id
        assert m1_run_id is not None

        cancelled = plan_coordinator.cancel_unregistered(
            pe_id, now=_NOW, heartbeat_window=_HEARTBEAT
        )
        assert cancelled.link(PlanMilestoneId("m1")).state is MilestoneRunState.REGISTERED
        assert cancelled.link(PlanMilestoneId("m1")).run_id == m1_run_id
        assert cancelled.link(PlanMilestoneId("m2a")).state is MilestoneRunState.CANCELLED
        assert cancelled.link(PlanMilestoneId("m2b")).state is MilestoneRunState.CANCELLED
        assert cancelled.link(PlanMilestoneId("m3")).state is MilestoneRunState.CANCELLED
        assert cancelled.state is PlanExecutionState.RUNNING  # m1 is still active


class TestResumedPlan:
    def test_a_fresh_coordinator_over_the_same_ledger_resumes_without_duplicating_children(
        self, ledger_service: LedgerService
    ) -> None:
        plan = load_plan(FIXTURES / "diamond.toml")
        first_plan_coordinator = _plan_coordinator(ledger_service)
        first_stack_coordinator = _stack_coordinator(ledger_service)
        pe_id = PlanExecutionId("pe-resumed")
        stack_id = StackId("stack-resumed")

        first_plan_coordinator.start(
            plan, plan_execution_id=pe_id, now=_NOW, heartbeat_window=_HEARTBEAT
        )
        _seed_stack(first_stack_coordinator, plan, stack_id)
        before_crash = first_plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        m1_run_id = before_crash.link(PlanMilestoneId("m1")).run_id
        assert m1_run_id is not None
        first_stack_coordinator.reconcile_after_publish(
            stack_id,
            PlanMilestoneId("m1"),
            head_revision="m1-rev1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )

        # Simulate a coordinator crash and replacement: fresh coordinators
        # over the same durable ledger, matching the recovery topology.
        second_plan_coordinator = _plan_coordinator(ledger_service)
        second_stack_coordinator = _stack_coordinator(ledger_service)

        after_resume = second_plan_coordinator.fan_out(
            plan,
            pe_id,
            now=_NOW + timedelta(seconds=90),
            heartbeat_window=_HEARTBEAT,
            child_run_state=_child_run_state,
        )
        assert after_resume.link(PlanMilestoneId("m1")).run_id == m1_run_id

        second_plan_coordinator.record_milestone_outcome(
            pe_id,
            PlanMilestoneId("m1"),
            MilestoneRunState.SUCCEEDED,
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
        resumed_execution = second_plan_coordinator.fan_out(
            plan, pe_id, now=_NOW, heartbeat_window=_HEARTBEAT, child_run_state=_child_run_state
        )
        assert resumed_execution.link(PlanMilestoneId("m2a")).state is MilestoneRunState.REGISTERED
        assert resumed_execution.link(PlanMilestoneId("m2b")).state is MilestoneRunState.REGISTERED

        stack_after_resume = second_stack_coordinator.read(stack_id)
        assert stack_after_resume is not None
        assert stack_after_resume.slice(PlanMilestoneId("m1")).head_revision == "m1-rev1"

        all_run_ids = [link.run_id for link in resumed_execution.milestones.values() if link.run_id]
        assert len(all_run_ids) == len(set(all_run_ids))  # no duplicate child run after resume
