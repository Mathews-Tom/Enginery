"""``ObservationRequest``: a declared watch for a Stage-1 subject's later,
not-yet-known real-world outcome.

Distinct from :class:`enginery.domain.outcome.Outcome`: an ``Outcome``
records only an already-observed fact and has no way to represent "not yet
observed" or "observed too late." An ``ObservationRequest`` tracks the
declared window during which a subject (a merged/rejected/abandoned pull
request, a reopened issue, an escaped defect) is still being watched for.
It resolves exactly once, to either ``captured`` (linking the resulting
``Outcome``) or ``indeterminate`` (the window elapsed with nothing
observed) -- both terminal and immutable, matching the domain-wide rule
that outcomes never rewrite prior history. A late or corrected observation
after expiry opens a new ``ObservationRequest`` rather than mutating the
expired one.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ObservationId, OutcomeId, RunId, WorkItemId
from enginery.domain.immutable import freeze_mapping
from enginery.domain.outcome import OutcomeKind


class ObservationState(enum.Enum):
    """The three closed states one observation request passes through."""

    PENDING = "pending"
    CAPTURED = "captured"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    """One declared watch for a Stage-1 subject's later real-world outcome."""

    id: ObservationId
    work_item_id: WorkItemId
    run_id: RunId
    kind: OutcomeKind
    opened_at: datetime
    window: timedelta
    state: ObservationState = ObservationState.PENDING
    resolved_at: datetime | None = None
    outcome_id: OutcomeId | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = field(default=1)

    def __post_init__(self) -> None:
        if self.opened_at.tzinfo is None:
            raise InvalidInputError("opened_at must be a timezone-aware datetime")
        if self.window <= timedelta(0):
            raise InvalidInputError("window must be a positive duration")
        if self.state is ObservationState.PENDING:
            if self.resolved_at is not None:
                raise InvalidInputError("a pending observation cannot carry resolved_at")
            if self.outcome_id is not None:
                raise InvalidInputError("a pending observation cannot carry outcome_id")
        else:
            if self.resolved_at is None:
                raise InvalidInputError(
                    f"a {self.state.value} observation requires resolved_at",
                    details={"state": self.state.value},
                )
            if self.resolved_at.tzinfo is None:
                raise InvalidInputError("resolved_at must be a timezone-aware datetime")
            if self.resolved_at < self.opened_at:
                raise InvalidInputError("resolved_at cannot precede opened_at")
        if self.state is ObservationState.CAPTURED and self.outcome_id is None:
            raise InvalidInputError("a captured observation requires outcome_id")
        if self.state is ObservationState.INDETERMINATE and self.outcome_id is not None:
            raise InvalidInputError("an indeterminate observation cannot carry outcome_id")
        if self.schema_version < 1:
            raise InvalidInputError(
                "schema_version must be at least 1",
                details={"schema_version": self.schema_version},
            )
        freeze_mapping(self, "detail", self.detail)

    @property
    def due_at(self) -> datetime:
        """The instant this observation's window elapses."""
        return self.opened_at + self.window

    def is_overdue(self, *, reference_time: datetime) -> bool:
        """True once ``reference_time`` reaches ``due_at`` and no capture landed first."""
        return self.state is ObservationState.PENDING and reference_time >= self.due_at

    def resolve_captured(
        self, *, outcome_id: OutcomeId, resolved_at: datetime
    ) -> ObservationRequest:
        """Resolve a pending observation to ``captured``, linking the resulting ``Outcome``."""
        self._require_pending()
        return replace(
            self, state=ObservationState.CAPTURED, resolved_at=resolved_at, outcome_id=outcome_id
        )

    def resolve_indeterminate(self, *, resolved_at: datetime) -> ObservationRequest:
        """Resolve a pending observation to ``indeterminate`` once its window has elapsed."""
        self._require_pending()
        if resolved_at < self.due_at:
            raise InvalidInputError(
                "an observation cannot become indeterminate before its window elapses",
                details={"due_at": self.due_at.isoformat(), "resolved_at": resolved_at.isoformat()},
            )
        return replace(self, state=ObservationState.INDETERMINATE, resolved_at=resolved_at)

    def _require_pending(self) -> None:
        if self.state is not ObservationState.PENDING:
            raise InvalidInputError(
                "only a pending observation can resolve", details={"state": self.state.value}
            )


__all__ = ["ObservationRequest", "ObservationState"]
