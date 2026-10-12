"""Source-bound terminal evidence for a Stage 1 merge-ready claim."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.workflows.pull_request import PullRequestOutcome


@dataclass(frozen=True, slots=True)
class Stage1EvidenceBundle:
    """Immutable terminal evidence bound to issue, base, and pull-request heads."""

    issue_revision: str
    base_revision: str
    head_revision: str
    pull_request_number: int
    implementation_artifacts: tuple[Digest, ...]
    verification_artifacts: tuple[Digest, ...]
    outcome: PullRequestOutcome
    observed_at: datetime
    execution_selection_digest: Digest | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.issue_revision, self.base_revision, self.head_revision)
        ):
            raise InvalidInputError("Stage 1 evidence revisions must be non-blank")
        if self.pull_request_number < 1:
            raise InvalidInputError("Stage 1 evidence pull request number must be positive")
        if self.outcome is PullRequestOutcome.MERGE_READY and (
            not self.implementation_artifacts or not self.verification_artifacts
        ):
            raise InvalidInputError(
                "merge-ready evidence requires implementation and verification artifacts"
            )
        if self.observed_at.tzinfo is None:
            raise InvalidInputError("Stage 1 evidence observation must be timezone-aware")

    @property
    def digest(self) -> Digest:
        payload: dict[str, object] = {
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
            "implementation_artifacts": [str(item) for item in self.implementation_artifacts],
            "issue_revision": self.issue_revision,
            "outcome": self.outcome.value,
            "pull_request_number": self.pull_request_number,
            "verification_artifacts": [str(item) for item in self.verification_artifacts],
        }
        if self.execution_selection_digest is not None:
            # Evidence produced under a bound execution selection is superseded by any
            # change to it. Evidence from a run that predates selections keeps its digest.
            payload["execution_selection_digest"] = str(self.execution_selection_digest)
        return Digest.of_json(payload)

    def current_for(
        self,
        *,
        issue_revision: str,
        base_revision: str,
        head_revision: str,
        execution_selection_digest: Digest | None = None,
    ) -> bool:
        """Return whether every terminal subject, including the execution selection, matches."""
        return (
            issue_revision == self.issue_revision
            and base_revision == self.base_revision
            and head_revision == self.head_revision
            and execution_selection_digest == self.execution_selection_digest
        )


__all__ = ["Stage1EvidenceBundle"]
