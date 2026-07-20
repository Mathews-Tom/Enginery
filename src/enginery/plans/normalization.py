"""Milestone normalization: turning ``PlanMilestone`` entries into ``WorkItem``s.

Every unit of engineering intent the coordinator ever runs, regardless of
provider, is a ``WorkItem``. A plan's milestones are no exception: this
module is the only place a ``PlanMilestone`` becomes a ``WorkItem``, so a
plan-sourced work item is indistinguishable in shape from an issue- or
incident-sourced one everywhere downstream of this boundary.

Normalization never mutates a ``Plan`` or executes anything; it is a pure
mapping from one immutable value to another. Work-item identity and
dependency references are derived deterministically from the plan's own
identity and each milestone's identity, so normalizing the same plan twice
always produces byte-identical ``WorkItem`` values.
"""

from __future__ import annotations

from enginery.domain.enums import WorkKind
from enginery.domain.ids import PlanMilestoneId, WorkItemId
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.plans.model import Plan, PlanMilestone


def work_item_id_for_milestone(plan: Plan, milestone_id: PlanMilestoneId) -> WorkItemId:
    """Derive the deterministic ``WorkItemId`` for one milestone of ``plan``."""
    return WorkItemId(f"{plan.id}:{milestone_id}")


def normalize_milestone(plan: Plan, milestone: PlanMilestone) -> WorkItem:
    """Normalize one ``PlanMilestone`` of ``plan`` into a new ``WorkItem``."""
    return WorkItem(
        id=work_item_id_for_milestone(plan, milestone.id),
        work_kind=WorkKind.MILESTONE,
        source_provider=plan.source_provider,
        external_reference=f"{plan.external_reference}#{milestone.id}",
        source_snapshot_reference=plan.source_snapshot_reference,
        title=milestone.title,
        objective=milestone.objective,
        acceptance_criteria=milestone.acceptance_criteria,
        constraints=(),
        risk_class=milestone.risk_class,
        repository_targets=(milestone.repository,),
        dependencies=tuple(
            work_item_id_for_milestone(plan, dependency) for dependency in milestone.dependencies
        ),
        state=WorkItemState.NEW,
    )


def normalize_plan(plan: Plan) -> tuple[WorkItem, ...]:
    """Normalize every milestone of ``plan`` into its ``WorkItem``, in
    deterministic topological order."""
    return tuple(
        normalize_milestone(plan, plan.milestone(milestone_id))
        for milestone_id in plan.topological_order()
    )


__all__ = ["normalize_milestone", "normalize_plan", "work_item_id_for_milestone"]
