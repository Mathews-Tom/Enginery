from __future__ import annotations

import pytest

from enginery.application.work_ports import (
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestReview,
    PullRequestSnapshot,
)
from enginery.domain.errors import InvalidInputError
from enginery.workflows.pull_request import (
    PullRequestOutcome,
    PullRequestRequirements,
    evaluate_pull_request,
)


def _evidence(
    *,
    head: str = "head-1",
    status: str = "completed",
    conclusion: str | None = "success",
    review: bool = True,
    mergeable: bool | None = True,
) -> PullRequestEvidence:
    pull_request = PullRequestSnapshot(
        1, "https://example.test/pr/1", "open", "work", head, "main", "base-1"
    )
    checks = (PullRequestCheck("CI", status, conclusion, head),)
    reviews = (PullRequestReview("reviewer", "APPROVED", head),) if review else ()
    return PullRequestEvidence(pull_request, reviews, checks, mergeable)


def _requirements() -> PullRequestRequirements:
    return PullRequestRequirements("head-1", ("CI",), True)


def test_exact_head_passing_ci_and_review_is_merge_ready() -> None:
    assert evaluate_pull_request(_evidence(), _requirements()) is PullRequestOutcome.MERGE_READY


def test_head_change_supersedes_prior_evidence() -> None:
    assert (
        evaluate_pull_request(_evidence(head="head-2"), _requirements())
        is PullRequestOutcome.SUPERSEDED
    )


@pytest.mark.parametrize("status, conclusion", [("queued", None), ("in_progress", None)])
def test_incomplete_exact_head_ci_waits(status: str, conclusion: str | None) -> None:
    assert (
        evaluate_pull_request(_evidence(status=status, conclusion=conclusion), _requirements())
        is PullRequestOutcome.WAITING
    )


def test_failing_exact_head_ci_blocks() -> None:
    assert (
        evaluate_pull_request(_evidence(conclusion="failure"), _requirements())
        is PullRequestOutcome.BLOCKED
    )


def test_missing_required_review_waits() -> None:
    assert (
        evaluate_pull_request(_evidence(review=False), _requirements())
        is PullRequestOutcome.WAITING
    )


def test_requirements_reject_empty_check_set() -> None:
    with pytest.raises(InvalidInputError, match="non-empty"):
        PullRequestRequirements("head-1", (), True)


def test_stale_review_evidence_is_rejected_at_the_port_boundary() -> None:
    pull_request = PullRequestSnapshot(
        1, "https://example.test/pr/1", "open", "work", "head-1", "main", "base-1"
    )
    with pytest.raises(ValueError, match="reviews must bind"):
        PullRequestEvidence(
            pull_request,
            (PullRequestReview("reviewer", "APPROVED", "head-0"),),
            (),
            True,
        )


def test_any_failed_duplicate_required_check_blocks() -> None:
    evidence = _evidence()
    failed = PullRequestCheck("CI", "completed", "failure", "head-1")
    with_duplicate = PullRequestEvidence(
        evidence.pull_request,
        evidence.reviews,
        (*evidence.checks, failed),
        evidence.mergeable,
    )

    assert evaluate_pull_request(with_duplicate, _requirements()) is PullRequestOutcome.BLOCKED
