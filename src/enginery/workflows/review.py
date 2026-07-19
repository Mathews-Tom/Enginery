"""Independent review outcomes and bounded repair routing."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError


class ReviewOutcome(enum.StrEnum):
    """Closed decisions from an independent review."""

    APPROVED = "approved"
    REPAIR_REQUESTED = "repair_requested"
    REJECTED = "rejected"
    REPAIR_EXHAUSTED = "repair_exhausted"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One actionable finding against an implementation attempt."""

    finding_id: str
    actionable: bool
    blocking: bool

    def __post_init__(self) -> None:
        if not self.finding_id.strip():
            raise InvalidInputError("review finding id must be non-blank")
        if self.blocking and not self.actionable:
            raise InvalidInputError("blocking review findings must be actionable")


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """A report produced by a reviewer independent of the implementation producer."""

    producer: str
    reviewer: str
    findings: tuple[ReviewFinding, ...]

    def __post_init__(self) -> None:
        if not self.producer.strip() or not self.reviewer.strip():
            raise InvalidInputError("review producer and reviewer must be non-blank")
        if self.producer == self.reviewer:
            raise InvalidInputError("implementation producer cannot review its own work")
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise InvalidInputError("review finding ids must be unique")


def route_review(report: ReviewReport, *, repair_attempt: int, repair_limit: int) -> ReviewOutcome:
    """Route one review to approval, rejection, or a bounded fresh repair attempt."""
    if repair_attempt < 0 or repair_limit < 0:
        raise InvalidInputError("review repair counts cannot be negative")
    if repair_attempt > repair_limit:
        raise InvalidInputError("review repair attempt exceeds configured limit")
    if any(finding.blocking for finding in report.findings):
        return ReviewOutcome.REJECTED
    if not any(finding.actionable for finding in report.findings):
        return ReviewOutcome.APPROVED
    if repair_attempt == repair_limit:
        return ReviewOutcome.REPAIR_EXHAUSTED
    return ReviewOutcome.REPAIR_REQUESTED


__all__ = ["ReviewFinding", "ReviewOutcome", "ReviewReport", "route_review"]
