"""``WorkItem``: a normalized unit of engineering intent.

This module declares the immutable aggregate, its closed lifecycle-state
vocabulary, and guarded transition enforcement built on the shared
``TransitionTable`` machinery.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace

from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import WorkItemId
from enginery.domain.state_machine import TransitionTable


class WorkItemState(enum.Enum):
    """The ten work-item lifecycle states."""

    NEW = "new"
    QUALIFYING = "qualifying"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED = "blocked"
    OUTCOME_PENDING = "outcome_pending"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


WORK_ITEM_TRANSITIONS: TransitionTable[WorkItemState] = TransitionTable(
    edges={
        WorkItemState.NEW: frozenset({WorkItemState.QUALIFYING}),
        WorkItemState.QUALIFYING: frozenset(
            {WorkItemState.READY, WorkItemState.BLOCKED, WorkItemState.REJECTED}
        ),
        WorkItemState.READY: frozenset({WorkItemState.ACTIVE, WorkItemState.CANCELLED}),
        WorkItemState.ACTIVE: frozenset(
            {
                WorkItemState.OUTCOME_PENDING,
                WorkItemState.BLOCKED,
                WorkItemState.CANCELLED,
                WorkItemState.FAILED,
            }
        ),
        WorkItemState.BLOCKED: frozenset(
            {
                WorkItemState.QUALIFYING,
                WorkItemState.ACTIVE,
                WorkItemState.REJECTED,
                WorkItemState.CANCELLED,
            }
        ),
        WorkItemState.OUTCOME_PENDING: frozenset(
            {WorkItemState.COMPLETED, WorkItemState.BLOCKED, WorkItemState.FAILED}
        ),
    },
    terminal_states=frozenset(
        {
            WorkItemState.COMPLETED,
            WorkItemState.REJECTED,
            WorkItemState.CANCELLED,
            WorkItemState.FAILED,
        }
    ),
)


@dataclass(frozen=True, slots=True)
class WorkItem:
    """A normalized, provider-neutral unit of engineering intent."""

    id: WorkItemId
    work_kind: WorkKind
    source_provider: str
    external_reference: str
    source_snapshot_reference: str
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    risk_class: RiskClass
    repository_targets: tuple[str, ...]
    dependencies: tuple[WorkItemId, ...]
    state: WorkItemState
    aggregate_version: int = field(default=0)

    def __post_init__(self) -> None:
        _require_non_blank(self.source_provider, field_name="source_provider")
        _require_non_blank(self.external_reference, field_name="external_reference")
        _require_non_blank(self.source_snapshot_reference, field_name="source_snapshot_reference")
        _require_non_blank(self.title, field_name="title")
        _require_non_blank(self.objective, field_name="objective")
        if not self.acceptance_criteria:
            raise InvalidInputError("a work item requires at least one acceptance criterion")
        if not self.repository_targets:
            raise InvalidInputError("a work item requires at least one repository target")
        if self.id in self.dependencies:
            raise InvalidInputError(
                "a work item cannot depend on itself", details={"work_item_id": str(self.id)}
            )
        if self.aggregate_version < 0:
            raise InvalidInputError(
                "aggregate_version cannot be negative",
                details={"aggregate_version": self.aggregate_version},
            )

    @property
    def bound_field_digest(self) -> Digest:
        """The deterministic digest checked for source supersession.

        Covers exactly the fields the design binds and rechecks before
        every human approval, side-effecting node, evidence-verification
        pass, and terminal transition: objective, acceptance criteria,
        constraints, dependencies, and repository targets.
        """
        return Digest.of_json(
            {
                "objective": self.objective,
                "acceptance_criteria": list(self.acceptance_criteria),
                "constraints": list(self.constraints),
                "dependencies": [str(dependency) for dependency in self.dependencies],
                "repository_targets": list(self.repository_targets),
            }
        )

    def transition_to(self, target: WorkItemState) -> WorkItem:
        """Return a new ``WorkItem`` in ``target`` state, or raise if the
        transition is not legal from the current state."""
        WORK_ITEM_TRANSITIONS.require(self.state, target)
        return replace(self, state=target, aggregate_version=self.aggregate_version + 1)


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


__all__ = ["WORK_ITEM_TRANSITIONS", "WorkItem", "WorkItemState"]
