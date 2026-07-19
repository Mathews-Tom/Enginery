from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.workflows.review import ReviewFinding, ReviewOutcome, ReviewReport, route_review


def _report(*findings: ReviewFinding) -> ReviewReport:
    return ReviewReport(producer="omp:run-1", reviewer="human:reviewer-1", findings=findings)


def test_review_approves_without_actionable_findings() -> None:
    assert route_review(_report(), repair_attempt=0, repair_limit=2) is ReviewOutcome.APPROVED


def test_review_requests_bounded_repair_for_actionable_finding() -> None:
    report = _report(ReviewFinding("finding-1", actionable=True, blocking=False))

    assert route_review(report, repair_attempt=1, repair_limit=2) is ReviewOutcome.REPAIR_REQUESTED


def test_review_exhausts_repair_budget_without_silent_approval() -> None:
    report = _report(ReviewFinding("finding-1", actionable=True, blocking=False))

    assert route_review(report, repair_attempt=2, repair_limit=2) is ReviewOutcome.REPAIR_EXHAUSTED


def test_blocking_review_finding_rejects_without_repair() -> None:
    report = _report(ReviewFinding("finding-1", actionable=True, blocking=True))

    assert route_review(report, repair_attempt=0, repair_limit=2) is ReviewOutcome.REJECTED


def test_producer_cannot_review_its_own_implementation() -> None:
    with pytest.raises(InvalidInputError, match="cannot review its own"):
        ReviewReport(producer="omp:run-1", reviewer="omp:run-1", findings=())


def test_review_rejects_attempts_beyond_configured_budget() -> None:
    with pytest.raises(InvalidInputError, match="exceeds"):
        route_review(_report(), repair_attempt=3, repair_limit=2)
