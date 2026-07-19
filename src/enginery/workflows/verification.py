"""Double-read verification for a Stage 1 merge-ready claim."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from enginery.application.work_ports import PullRequestPort, WorkLedgerPort, WorkLedgerSnapshot
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.workflows.evidence import Stage1EvidenceBundle
from enginery.workflows.pull_request import (
    PullRequestOutcome,
    PullRequestRequirements,
    evaluate_pull_request,
)


@dataclass(frozen=True, slots=True)
class Stage1VerificationRequest:
    """Immutable subjects required by one terminal merge-ready evaluation."""

    external_reference: str
    issue_revision: str
    issue_digest: str
    base_revision: str
    pull_request_number: int
    requirements: PullRequestRequirements
    implementation_artifacts: tuple[Digest, ...]
    verification_artifacts: tuple[Digest, ...]

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.external_reference,
                self.issue_revision,
                self.issue_digest,
                self.base_revision,
            )
        ):
            raise InvalidInputError("Stage 1 verification subjects must be non-blank")
        if self.pull_request_number < 1:
            raise InvalidInputError("Stage 1 verification pull request number must be positive")


@dataclass(frozen=True, slots=True)
class Stage1VerificationResult:
    """A closed verification outcome and its terminal bundle when merge-ready."""

    outcome: PullRequestOutcome
    evidence: Stage1EvidenceBundle | None

    def __post_init__(self) -> None:
        if self.outcome is PullRequestOutcome.MERGE_READY and self.evidence is None:
            raise InvalidInputError("merge-ready verification requires terminal evidence")
        if self.outcome is not PullRequestOutcome.MERGE_READY and self.evidence is not None:
            raise InvalidInputError("non-merge-ready verification cannot emit terminal evidence")


@dataclass(frozen=True, slots=True)
class Stage1VerificationExecutor:
    """Verify exact current subjects twice before emitting terminal evidence."""

    work_ledger: WorkLedgerPort
    pull_requests: PullRequestPort

    def verify(
        self, *, request: Stage1VerificationRequest, observed_at: datetime
    ) -> Stage1VerificationResult:
        """Evaluate current source and PR state twice without merging the pull request."""
        first_issue = self.work_ledger.fetch(request.external_reference)
        first_evidence = self.pull_requests.evidence(request.pull_request_number)
        if not _matches_request(request, first_issue, first_evidence.pull_request.base_revision):
            return Stage1VerificationResult(PullRequestOutcome.SUPERSEDED, None)
        first_outcome = evaluate_pull_request(first_evidence, request.requirements)
        if first_outcome is not PullRequestOutcome.MERGE_READY:
            return Stage1VerificationResult(first_outcome, None)

        terminal_issue = self.work_ledger.fetch(request.external_reference)
        terminal_evidence = self.pull_requests.evidence(request.pull_request_number)
        if not _matches_request(
            request, terminal_issue, terminal_evidence.pull_request.base_revision
        ):
            return Stage1VerificationResult(PullRequestOutcome.SUPERSEDED, None)
        terminal_outcome = evaluate_pull_request(terminal_evidence, request.requirements)
        if terminal_outcome is not PullRequestOutcome.MERGE_READY:
            return Stage1VerificationResult(terminal_outcome, None)

        return Stage1VerificationResult(
            PullRequestOutcome.MERGE_READY,
            Stage1EvidenceBundle(
                issue_revision=request.issue_revision,
                base_revision=request.base_revision,
                head_revision=request.requirements.expected_head_revision,
                pull_request_number=request.pull_request_number,
                implementation_artifacts=request.implementation_artifacts,
                verification_artifacts=request.verification_artifacts,
                outcome=PullRequestOutcome.MERGE_READY,
                observed_at=observed_at,
            ),
        )


def _matches_request(
    request: Stage1VerificationRequest,
    issue: WorkLedgerSnapshot,
    base_revision: str,
) -> bool:
    return (
        issue.source_revision == request.issue_revision
        and str(issue.work_item.bound_field_digest) == request.issue_digest
        and base_revision == request.base_revision
    )


__all__ = [
    "Stage1VerificationExecutor",
    "Stage1VerificationRequest",
    "Stage1VerificationResult",
]
