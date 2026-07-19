"""Exact-head pull-request evidence evaluation for Stage 1."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from enginery.application.work_ports import PullRequestEvidence
from enginery.domain.errors import InvalidInputError


class PullRequestOutcome(enum.StrEnum):
    """Closed readiness outcome for the observed pull-request head."""

    MERGE_READY = "merge_ready"
    WAITING = "waiting"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class PullRequestRequirements:
    """Required exact-head CI and independent-review observations."""

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
    """Classify a PR only from evidence tied to the expected current head."""
    pull_request = evidence.pull_request
    if pull_request.head_revision != requirements.expected_head_revision:
        return PullRequestOutcome.SUPERSEDED
    if pull_request.state.lower() != "open" or evidence.mergeable is False:
        return PullRequestOutcome.BLOCKED
    checks_by_name: dict[str, list[str | None]] = {}
    statuses_by_name: dict[str, list[str]] = {}
    for check in evidence.checks:
        checks_by_name.setdefault(check.name, []).append(check.conclusion)
        statuses_by_name.setdefault(check.name, []).append(check.status)
    if any(name not in checks_by_name for name in requirements.required_checks):
        return PullRequestOutcome.WAITING
    for name in requirements.required_checks:
        statuses = statuses_by_name[name]
        conclusions = checks_by_name[name]
        if any(status.lower() != "completed" for status in statuses):
            return PullRequestOutcome.WAITING
        if any(conclusion is None or conclusion.lower() != "success" for conclusion in conclusions):
            return PullRequestOutcome.BLOCKED
    if requirements.require_approved_review and not any(
        review.state.upper() == "APPROVED" for review in evidence.reviews
    ):
        return PullRequestOutcome.WAITING
    if evidence.mergeable is None:
        return PullRequestOutcome.WAITING
    return PullRequestOutcome.MERGE_READY


__all__ = ["PullRequestOutcome", "PullRequestRequirements", "evaluate_pull_request"]
