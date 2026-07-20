#!/usr/bin/env python3
"""Stress plan-to-child-run fan-out, join, and stack reconciliation.

Drives a real ``Plan`` fixture through the real SQLite-backed
``PlanExecutionCoordinator`` and ``StackCoordinator``, injecting a
coordinator restart (a fresh ``CoordinatorRuntime``/coordinator pair
over the same ledger, exactly like a real process replacement) at
random ticks and constraining concurrency to a caller-supplied budget.
Reports whether any milestone's child run was ever registered twice.
"""

from __future__ import annotations

import argparse
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from enginery.domain.digests import Digest
from enginery.domain.ids import PlanExecutionId, PlanMilestoneId, RunId, StackId
from enginery.domain.plan_execution import MilestoneRunState
from enginery.engine.plan_execution import PlanExecutionCoordinator
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService
from enginery.plans.loader import load_plan
from enginery.plans.model import Plan

_TERMINAL_MILESTONE_STATES = frozenset(
    {
        MilestoneRunState.SUCCEEDED,
        MilestoneRunState.FAILED,
        MilestoneRunState.BLOCKED,
        MilestoneRunState.CANCELLED,
    }
)


def _child_run_state(plan: Plan, milestone_id: PlanMilestoneId, run_id: RunId) -> dict[str, object]:
    return {"run_id": str(run_id), "plan_id": str(plan.id), "milestone_id": str(milestone_id)}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--interruptions", type=int, default=2)
    parser.add_argument("--global-concurrency", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    randomizer = random.Random(arguments.seed)
    plan = load_plan(arguments.fixture)
    limits = SchedulingLimits(
        global_concurrency=arguments.global_concurrency, per_repository_concurrency=5
    )
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    heartbeat = timedelta(seconds=30)

    with TemporaryDirectory() as directory:
        database = Path(directory) / "ledger.db"
        ledger = LedgerService.open(database)
        try:
            plan_execution_id = PlanExecutionId("stress-plan-execution")
            stack_id = StackId("stress-stack")

            def _fresh_plan_coordinator() -> PlanExecutionCoordinator:
                return PlanExecutionCoordinator(
                    ledger, CoordinatorRuntime(ledger, owner="stress-coordinator")
                )

            def _fresh_stack_coordinator() -> StackCoordinator:
                return StackCoordinator(
                    ledger, CoordinatorRuntime(ledger, owner="stress-coordinator")
                )

            plan_coordinator = _fresh_plan_coordinator()
            stack_coordinator = _fresh_stack_coordinator()

            plan_coordinator.start(
                plan, plan_execution_id=plan_execution_id, now=now, heartbeat_window=heartbeat
            )
            stack_coordinator.start(
                stack_id=stack_id,
                plan_id=plan.id,
                base_ref="main",
                ordered_milestones=[
                    (milestone_id, f"feature/{milestone_id}")
                    for milestone_id in plan.topological_order()
                ],
                now=now,
                heartbeat_window=heartbeat,
            )

            all_registered_run_ids: set[str] = set()
            duplicate_registrations = 0
            coordinator_restarts = 0
            ticks = 0
            max_ticks = len(plan.milestones) * 4 + 10

            while True:
                execution = plan_coordinator.read(plan_execution_id)
                assert execution is not None
                if all(
                    link.state in _TERMINAL_MILESTONE_STATES
                    for link in execution.milestones.values()
                ):
                    break
                if ticks >= max_ticks:
                    raise RuntimeError("plan stress did not converge within the tick budget")
                ticks += 1
                now += timedelta(seconds=1)

                if coordinator_restarts < arguments.interruptions and randomizer.random() < 0.5:
                    plan_coordinator = _fresh_plan_coordinator()
                    stack_coordinator = _fresh_stack_coordinator()
                    coordinator_restarts += 1

                before = plan_coordinator.read(plan_execution_id)
                assert before is not None
                after = plan_coordinator.fan_out_within_limits(
                    plan,
                    plan_execution_id,
                    now=now,
                    heartbeat_window=heartbeat,
                    child_run_state=_child_run_state,
                    limits=limits,
                )
                for milestone_id, link in after.milestones.items():
                    if (
                        link.state is MilestoneRunState.REGISTERED
                        and before.link(milestone_id).state is MilestoneRunState.PENDING
                    ):
                        assert link.run_id is not None
                        run_id_str = str(link.run_id)
                        if run_id_str in all_registered_run_ids:
                            duplicate_registrations += 1
                        all_registered_run_ids.add(run_id_str)
                        branch_head = f"{milestone_id}-{run_id_str[:12]}"
                        stack_coordinator.reconcile_after_publish(
                            stack_id,
                            milestone_id,
                            head_revision=branch_head,
                            now=now,
                            heartbeat_window=heartbeat,
                        )
                        stack_coordinator.mark_merge_ready(
                            stack_id,
                            milestone_id,
                            head_revision=branch_head,
                            ci_evidence_digest=Digest.of_bytes(branch_head.encode("utf-8")),
                            now=now,
                            heartbeat_window=heartbeat,
                        )

                current = plan_coordinator.read(plan_execution_id)
                assert current is not None
                for milestone_id, link in current.milestones.items():
                    if link.state is MilestoneRunState.REGISTERED:
                        plan_coordinator.record_milestone_outcome(
                            plan_execution_id,
                            milestone_id,
                            MilestoneRunState.SUCCEEDED,
                            now=now,
                            heartbeat_window=heartbeat,
                        )

            final_execution = plan_coordinator.read(plan_execution_id)
            assert final_execution is not None
            merged = 0
            while True:
                stack = stack_coordinator.read(stack_id)
                assert stack is not None
                candidate = stack.next_mergeable()
                if candidate is None:
                    break
                stack_coordinator.mark_merged(
                    stack_id, candidate, now=now, heartbeat_window=heartbeat
                )
                merged += 1

            final_stack = stack_coordinator.read(stack_id)
            assert final_stack is not None
            print(
                f"fixture={arguments.fixture} milestones={len(plan.milestones)} ticks={ticks} "
                f"coordinator_restarts={coordinator_restarts} "
                f"duplicate_registrations={duplicate_registrations} "
                f"final_plan_state={final_execution.state.value} "
                f"merged_slices={merged}/{len(final_stack.slices)}"
            )
            success = (
                duplicate_registrations == 0
                and final_execution.state.value == "succeeded"
                and merged == len(final_stack.slices)
            )
            return 0 if success else 1
        finally:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
