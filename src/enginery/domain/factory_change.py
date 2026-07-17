"""``FactoryChange``: a candidate change to a workflow-system asset.

Declares the aggregate, its nine-state lifecycle vocabulary, and guarded
transition enforcement. "Active workflows are immutable": a
``FactoryChange`` always proposes a distinct ``candidate_version`` next to
the unchanged ``baseline_version`` rather than editing it in place.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import FactoryChangeId
from enginery.domain.immutable import freeze_mapping
from enginery.domain.state_machine import TransitionTable


class FactoryChangeState(enum.Enum):
    """The nine factory-change lifecycle states."""

    PROPOSED = "proposed"
    EVALUATION_READY = "evaluation_ready"
    EVALUATING = "evaluating"
    REVIEW_REQUIRED = "review_required"
    CANARY_READY = "canary_ready"
    CANARYING = "canarying"
    PROMOTED = "promoted"
    RETAINED = "retained"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


FACTORY_CHANGE_TRANSITIONS: TransitionTable[FactoryChangeState] = TransitionTable(
    edges={
        FactoryChangeState.PROPOSED: frozenset(
            {FactoryChangeState.EVALUATION_READY, FactoryChangeState.REJECTED}
        ),
        FactoryChangeState.EVALUATION_READY: frozenset({FactoryChangeState.EVALUATING}),
        FactoryChangeState.EVALUATING: frozenset({FactoryChangeState.REVIEW_REQUIRED}),
        FactoryChangeState.REVIEW_REQUIRED: frozenset(
            {FactoryChangeState.CANARY_READY, FactoryChangeState.REJECTED}
        ),
        FactoryChangeState.CANARY_READY: frozenset({FactoryChangeState.CANARYING}),
        FactoryChangeState.CANARYING: frozenset(
            {
                FactoryChangeState.PROMOTED,
                FactoryChangeState.RETAINED,
                FactoryChangeState.ROLLED_BACK,
            }
        ),
        FactoryChangeState.RETAINED: frozenset(
            {
                FactoryChangeState.EVALUATION_READY,
                FactoryChangeState.CANARY_READY,
                FactoryChangeState.REJECTED,
            }
        ),
    },
    terminal_states=frozenset(
        {FactoryChangeState.PROMOTED, FactoryChangeState.REJECTED, FactoryChangeState.ROLLED_BACK}
    ),
)


@dataclass(frozen=True, slots=True)
class FactoryChange:
    """A candidate change to a workflow-system asset, never an in-place edit."""

    id: FactoryChangeId
    affected_asset: str
    baseline_version: str
    problem_statement: str
    hypothesis: str
    candidate_version: str
    state: FactoryChangeState
    evaluation_set_digest: str | None = None
    comparison_result: Mapping[str, object] | None = None
    approval_state: str | None = None
    canary_cohort: tuple[str, ...] = field(default_factory=tuple)
    promotion_result: str | None = None
    aggregate_version: int = field(default=0)

    def __post_init__(self) -> None:
        _require_non_blank(self.affected_asset, field_name="affected_asset")
        _require_non_blank(self.baseline_version, field_name="baseline_version")
        _require_non_blank(self.problem_statement, field_name="problem_statement")
        _require_non_blank(self.hypothesis, field_name="hypothesis")
        _require_non_blank(self.candidate_version, field_name="candidate_version")
        if self.candidate_version == self.baseline_version:
            raise InvalidInputError(
                "candidate_version must differ from baseline_version; "
                "active factory assets cannot be edited in place",
                details={"baseline_version": self.baseline_version},
            )
        if self.aggregate_version < 0:
            raise InvalidInputError(
                "aggregate_version cannot be negative",
                details={"aggregate_version": self.aggregate_version},
            )
        if self.comparison_result is not None:
            freeze_mapping(self, "comparison_result", self.comparison_result)

    def transition_to(self, target: FactoryChangeState) -> FactoryChange:
        """Return a new ``FactoryChange`` in ``target`` state, or raise if the
        transition is not legal from the current state."""
        FACTORY_CHANGE_TRANSITIONS.require(self.state, target)
        return replace(self, state=target, aggregate_version=self.aggregate_version + 1)


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


__all__ = ["FACTORY_CHANGE_TRANSITIONS", "FactoryChange", "FactoryChangeState"]
