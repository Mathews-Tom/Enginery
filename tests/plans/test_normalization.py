"""Tests for enginery.plans.normalization."""

from __future__ import annotations

from pathlib import Path

from enginery.domain.enums import WorkKind
from enginery.domain.ids import PlanMilestoneId
from enginery.domain.work_item import WorkItemState
from enginery.plans.loader import load_plan
from enginery.plans.normalization import normalize_plan, work_item_id_for_milestone

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plans"


def test_normalize_plan_produces_one_work_item_per_milestone_in_topological_order() -> None:
    plan = load_plan(FIXTURES / "diamond.toml")
    work_items = normalize_plan(plan)
    assert [item.id for item in work_items] == [
        work_item_id_for_milestone(plan, milestone_id) for milestone_id in plan.topological_order()
    ]
    for item in work_items:
        assert item.work_kind is WorkKind.MILESTONE
        assert item.state is WorkItemState.NEW


def test_normalize_plan_preserves_milestone_dependencies_as_work_item_dependencies() -> None:
    plan = load_plan(FIXTURES / "diamond.toml")
    work_items = {item.id: item for item in normalize_plan(plan)}
    m3 = work_items[work_item_id_for_milestone(plan, PlanMilestoneId("m3"))]
    expected_dependencies = {
        work_item_id_for_milestone(plan, PlanMilestoneId("m2a")),
        work_item_id_for_milestone(plan, PlanMilestoneId("m2b")),
    }
    assert set(m3.dependencies) == expected_dependencies


def test_normalize_milestone_is_deterministic_across_calls() -> None:
    plan = load_plan(FIXTURES / "linear.toml")
    first = normalize_plan(plan)
    second = normalize_plan(plan)
    assert first == second


def test_work_item_id_for_milestone_is_namespaced_by_plan() -> None:
    plan = load_plan(FIXTURES / "linear.toml")
    other_plan = load_plan(FIXTURES / "parallel.toml")
    assert work_item_id_for_milestone(plan, PlanMilestoneId("m1")) != work_item_id_for_milestone(
        other_plan, PlanMilestoneId("m1")
    )
