"""Exact-head pull-request evidence evaluation."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.application.work_ports import PullRequestEvidence, PullRequestReview
from enginery.domain.errors import InvalidInputError


class PullRequestOutcome(enum.StrEnum):
    """Closed readiness result for one observed pull-request head."""

    MERGE_READY = "merge_ready"
    WAITING = "waiting"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class PullRequestRequirements:
    """Exact-head CI and independent-review requirements."""

    expected_head_revision: str
    required_checks: tuple[str, ...]
    require_approved_review: bool

    def __post_init__(self) -> None:
        if not self.expected_head_revision.strip():
            raise InvalidInputError("expected pull-request head revision must be non-blank")
        if not self.required_checks or any(not check.strip() for check in self.required_checks):
            raise InvalidInputError("required pull-request checks must be non-empty names")
        if len(self.required_checks) != len(set(self.required_checks)):
            raise InvalidInputError("required pull-request checks must be unique")


def evaluate_pull_request(
    evidence: PullRequestEvidence, requirements: PullRequestRequirements
) -> PullRequestOutcome:
    """Classify a PR only from current exact-head evidence."""
    pull_request = evidence.pull_request
    if pull_request.head_revision != requirements.expected_head_revision:
        return PullRequestOutcome.SUPERSEDED
    if pull_request.state.lower() != "open" or evidence.mergeable is False:
        return PullRequestOutcome.BLOCKED
    if not evidence.complete:
        return PullRequestOutcome.BLOCKED
    if evidence.combined_status in {"failure", "error"} or any(
        status.state.lower() in {"failure", "error"} for status in evidence.statuses
    ):
        return PullRequestOutcome.BLOCKED
    latest_reviews: dict[str, tuple[tuple[datetime, int], PullRequestReview]] = {}
    for index, review in enumerate(evidence.reviews):
        if review.state.upper() not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        # Live evidence carries timezone-aware submission times and IDs. Historical
        # normalized fixtures retain their input ordering when those fields are absent.
        try:
            timestamp = (
                datetime.fromisoformat(review.submitted_at.replace("Z", "+00:00"))
                if review.submitted_at
                else datetime.min.replace(tzinfo=UTC)
            )
        except ValueError:
            return PullRequestOutcome.BLOCKED
        if timestamp.tzinfo is None:
            return PullRequestOutcome.BLOCKED
        key = (timestamp, review.review_id if review.review_id is not None else index)
        reviewer = review.reviewer.casefold()
        previous = latest_reviews.get(reviewer)
        if previous is None or key > previous[0]:
            latest_reviews[reviewer] = (key, review)
    if any(review.state.upper() == "CHANGES_REQUESTED" for _, review in latest_reviews.values()):
        return PullRequestOutcome.BLOCKED
    if any(
        check.conclusion
        in {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
        for check in evidence.checks
    ):
        return PullRequestOutcome.BLOCKED
    if (evidence.statuses and evidence.combined_status not in {None, "success"}) or any(
        status.state.lower() != "success" for status in evidence.statuses
    ):
        return PullRequestOutcome.WAITING
    checks_by_name: dict[str, list[tuple[str, str | None]]] = {}
    for check in evidence.checks:
        checks_by_name.setdefault(check.name, []).append((check.status, check.conclusion))
    for status in evidence.statuses:
        checks_by_name.setdefault(status.context, []).append(
            ("completed" if status.state.lower() == "success" else status.state, status.state)
        )
    if any(name not in checks_by_name for name in requirements.required_checks):
        return PullRequestOutcome.WAITING
    for name in requirements.required_checks:
        if any(status.lower() != "completed" for status, _ in checks_by_name[name]):
            return PullRequestOutcome.WAITING
        if any(
            conclusion is None or conclusion.lower() != "success"
            for _, conclusion in checks_by_name[name]
        ):
            return PullRequestOutcome.BLOCKED
    if requirements.require_approved_review and not any(
        review.state.upper() == "APPROVED"
        and review.head_revision == requirements.expected_head_revision
        for _, review in latest_reviews.values()
    ):
        return PullRequestOutcome.WAITING
    if evidence.mergeable is None:
        return PullRequestOutcome.WAITING
    return PullRequestOutcome.MERGE_READY


__all__ = ["PullRequestOutcome", "PullRequestRequirements", "evaluate_pull_request"]
