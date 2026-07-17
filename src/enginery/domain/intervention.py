"""``Intervention``: a human action linked to a run (03_SYSTEM_DESIGN.md §9.7)."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import InterventionId, RunId
from enginery.domain.immutable import freeze_mapping


class InterventionKind(enum.Enum):
    """The seven intervention kinds named in §9.7."""

    APPROVAL = "approval"
    REJECTION = "rejection"
    CORRECTION = "correction"
    SUPPLIED_FACT = "supplied_fact"
    WAIVER = "waiver"
    OVERRIDE = "override"
    MANUAL_EXTERNAL_ACTION = "manual_external_action"


@dataclass(frozen=True, slots=True)
class Intervention:
    """A human approval, rejection, correction, fact, waiver, override, or
    manual external action, retained as an evaluation signal."""

    id: InterventionId
    kind: InterventionKind
    run_id: RunId
    actor: str
    occurred_at: datetime
    rationale: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.actor.strip():
            raise InvalidInputError("actor must be a non-blank string")
        if not self.rationale.strip():
            raise InvalidInputError("rationale must be a non-blank string")
        if self.occurred_at.tzinfo is None:
            raise InvalidInputError("occurred_at must be a timezone-aware datetime")
        freeze_mapping(self, "detail", self.detail)


__all__ = ["Intervention", "InterventionKind"]
