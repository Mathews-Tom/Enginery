"""Tests for enginery.plans.model."""

from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.enums import RiskClass
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PlanId, PlanMilestoneId
from enginery.plans.loader import load_plan
from enginery.plans.model import Plan, PlanMilestone

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plans"


def _milestone(milestone_id: str, *, dependencies: tuple[str, ...] = ()) -> PlanMilestone:
    return PlanMilestone(
        id=PlanMilestoneId(milestone_id),
        title=f"Milestone {milestone_id}",
        objective="objective",
        acceptance_criteria=("criterion",),
        repository="repo-a",
        risk_class=RiskClass.LOW,
        dependencies=tuple(PlanMilestoneId(value) for value in dependencies),
    )


def test_linear_plan_parses_and_orders_by_dependency() -> None:
    plan = load_plan(FIXTURES / "linear.toml")
    order = plan.topological_order()
    assert order == (PlanMilestoneId("m1"), PlanMilestoneId("m2"), PlanMilestoneId("m3"))


def test_diamond_plan_orders_join_after_both_branches() -> None:
    plan = load_plan(FIXTURES / "diamond.toml")
    order = plan.topological_order()
    assert order[0] == PlanMilestoneId("m1")
    assert order[-1] == PlanMilestoneId("m3")
    assert set(order[1:3]) == {PlanMilestoneId("m2a"), PlanMilestoneId("m2b")}


def test_parallel_plan_has_no_forced_order_between_independent_milestones() -> None:
    plan = load_plan(FIXTURES / "parallel.toml")
    order = plan.topological_order()
    assert set(order) == {PlanMilestoneId("m1"), PlanMilestoneId("m2"), PlanMilestoneId("m3")}


def test_cycle_with_no_entry_milestone_fails_before_execution() -> None:
    with pytest.raises(InvalidInputError, match="every milestone participates in a cycle"):
        load_plan(FIXTURES / "cycle.toml")


def test_cycle_unreachable_from_a_valid_entry_milestone_fails_before_execution() -> None:
    with pytest.raises(InvalidInputError, match="dependency cycle"):
        load_plan(FIXTURES / "cycle_with_entry.toml")


def test_unresolved_dependency_fails_before_execution() -> None:
    with pytest.raises(InvalidInputError, match="unresolved milestone"):
        load_plan(FIXTURES / "unresolved_dependency.toml")


def test_plan_rejects_duplicate_milestone_ids() -> None:
    with pytest.raises(InvalidInputError, match="duplicate milestone ids"):
        Plan(
            id=PlanId("plan-dup"),
            source_provider="fixture",
            external_reference="dup-1",
            source_snapshot_reference="sha:dup",
            milestones=(_milestone("m1"), _milestone("m1")),
        )


def test_milestone_self_dependency_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="cannot depend on itself"):
        _milestone("m1", dependencies=("m1",))


def test_plan_requires_at_least_one_milestone() -> None:
    with pytest.raises(InvalidInputError, match="at least one milestone"):
        Plan(
            id=PlanId("plan-empty"),
            source_provider="fixture",
            external_reference="empty-1",
            source_snapshot_reference="sha:empty",
            milestones=(),
        )


def test_plan_round_trips_through_mapping() -> None:
    plan = load_plan(FIXTURES / "diamond.toml")
    rebuilt = Plan.from_mapping(plan.to_mapping())
    assert rebuilt == plan
    assert rebuilt.content_digest == plan.content_digest


def test_plan_content_digest_is_stable_across_equivalent_construction() -> None:
    first = load_plan(FIXTURES / "linear.toml")
    second = load_plan(FIXTURES / "linear.toml")
    assert first.content_digest == second.content_digest


def test_plan_rejects_unknown_top_level_keys() -> None:
    with pytest.raises(InvalidInputError, match="unknown keys"):
        Plan.from_mapping(
            {
                "id": "plan-x",
                "source_provider": "fixture",
                "external_reference": "x-1",
                "source_snapshot_reference": "sha:x",
                "milestones": [
                    {
                        "id": "m1",
                        "title": "t",
                        "objective": "o",
                        "acceptance_criteria": ["c"],
                        "repository": "repo-a",
                        "risk_class": "low",
                        "dependencies": [],
                    }
                ],
                "shell_command": "rm -rf /",
            }
        )


def test_plan_rejects_unknown_risk_class() -> None:
    with pytest.raises(InvalidInputError, match="risk_class"):
        Plan.from_mapping(
            {
                "id": "plan-x",
                "source_provider": "fixture",
                "external_reference": "x-1",
                "source_snapshot_reference": "sha:x",
                "milestones": [
                    {
                        "id": "m1",
                        "title": "t",
                        "objective": "o",
                        "acceptance_criteria": ["c"],
                        "repository": "repo-a",
                        "risk_class": "catastrophic",
                        "dependencies": [],
                    }
                ],
            }
        )
