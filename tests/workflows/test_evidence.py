from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.workflows.evidence import Stage1EvidenceBundle
from enginery.workflows.pull_request import PullRequestOutcome


def test_merge_ready_evidence_requires_implementation_and_verification_artifacts() -> None:
    with pytest.raises(InvalidInputError, match="requires implementation"):
        Stage1EvidenceBundle(
            issue_revision="issue",
            base_revision="base",
            head_revision="head",
            pull_request_number=1,
            implementation_artifacts=(),
            verification_artifacts=(),
            outcome=PullRequestOutcome.MERGE_READY,
            observed_at=datetime(2026, 7, 19, tzinfo=UTC),
        )


def test_evidence_digest_is_current_only_for_the_bound_subjects() -> None:
    evidence = Stage1EvidenceBundle(
        issue_revision="issue",
        base_revision="base",
        head_revision="head",
        pull_request_number=1,
        implementation_artifacts=(Digest.of_bytes(b"implementation"),),
        verification_artifacts=(Digest.of_bytes(b"verification"),),
        outcome=PullRequestOutcome.MERGE_READY,
        observed_at=datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert evidence.current_for(issue_revision="issue", base_revision="base", head_revision="head")
    assert not evidence.current_for(
        issue_revision="changed", base_revision="base", head_revision="head"
    )
    assert str(evidence.digest).startswith("sha256:")
