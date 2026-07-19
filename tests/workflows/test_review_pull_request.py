from __future__ import annotations

from enginery.application.work_ports import (
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestReview,
    PullRequestSnapshot,
)
from enginery.workflows.pull_request import (
    PullRequestOutcome,
    PullRequestRequirements,
    evaluate_pull_request,
)
from enginery.workflows.review import ReviewFinding, ReviewOutcome, ReviewReport, route_review


def test_review_routes_actionable_findings_to_a_fresh_bounded_attempt() -> None:
    report = ReviewReport(
        producer="omp",
        reviewer="human",
        findings=(ReviewFinding("finding-1", actionable=True, blocking=False),),
    )

    assert route_review(report, repair_attempt=0, repair_limit=1) is ReviewOutcome.REPAIR_REQUESTED
    assert route_review(report, repair_attempt=1, repair_limit=1) is ReviewOutcome.REPAIR_EXHAUSTED


def test_review_approval_requires_no_actionable_findings() -> None:
    report = ReviewReport(producer="omp", reviewer="human", findings=())

    assert route_review(report, repair_attempt=0, repair_limit=1) is ReviewOutcome.APPROVED


def test_pr_evaluation_rejects_stale_head_evidence() -> None:
    snapshot = PullRequestSnapshot(
        number=1,
        url="https://example.invalid/pull/1",
        state="open",
        head_branch="feature",
        head_revision="current-head",
        base_branch="main",
        base_revision="base",
    )
    evidence = PullRequestEvidence(
        pull_request=snapshot,
        reviews=(PullRequestReview("reviewer", "APPROVED", "current-head"),),
        checks=(PullRequestCheck("CI", "completed", "success", "current-head"),),
        mergeable=True,
    )
    requirements = PullRequestRequirements(
        expected_head_revision="stale-head", required_checks=("CI",), require_approved_review=True
    )

    assert evaluate_pull_request(evidence, requirements) is PullRequestOutcome.SUPERSEDED


def test_pr_evaluation_requires_exact_head_ci_and_review() -> None:
    snapshot = PullRequestSnapshot(
        number=1,
        url="https://example.invalid/pull/1",
        state="open",
        head_branch="feature",
        head_revision="head",
        base_branch="main",
        base_revision="base",
    )
    evidence = PullRequestEvidence(
        pull_request=snapshot,
        reviews=(PullRequestReview("reviewer", "APPROVED", "head"),),
        checks=(PullRequestCheck("CI", "completed", "success", "head"),),
        mergeable=True,
    )
    requirements = PullRequestRequirements(
        expected_head_revision="head", required_checks=("CI",), require_approved_review=True
    )

    assert evaluate_pull_request(evidence, requirements) is PullRequestOutcome.MERGE_READY


def test_pr_evaluation_rejects_approval_for_a_superseded_head() -> None:
    snapshot = PullRequestSnapshot(
        number=1,
        url="https://example.invalid/pull/1",
        state="open",
        head_branch="feature",
        head_revision="head",
        base_branch="main",
        base_revision="base",
    )
    evidence = PullRequestEvidence(
        pull_request=snapshot,
        reviews=(PullRequestReview("reviewer", "APPROVED", "prior-head"),),
        checks=(PullRequestCheck("CI", "completed", "success", "head"),),
        mergeable=True,
    )
    requirements = PullRequestRequirements(
        expected_head_revision="head", required_checks=("CI",), require_approved_review=True
    )

    assert evaluate_pull_request(evidence, requirements) is PullRequestOutcome.WAITING
