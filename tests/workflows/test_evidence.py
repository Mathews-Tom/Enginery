from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.workflows.evidence import Stage1EvidenceBundle
from enginery.workflows.pull_request import PullRequestOutcome


def _digest(value: str) -> Digest:
    return Digest.of_bytes(value.encode())


def _bundle(outcome: PullRequestOutcome = PullRequestOutcome.MERGE_READY) -> Stage1EvidenceBundle:
    return Stage1EvidenceBundle(
        issue_revision="issue-v1",
        base_revision="base-v1",
        head_revision="head-v1",
        pull_request_number=1,
        implementation_artifacts=(_digest("implementation"),),
        verification_artifacts=(_digest("verification"),),
        outcome=outcome,
        observed_at=datetime.now(UTC),
    )


def test_merge_ready_evidence_is_current_only_for_exact_subject_versions() -> None:
    bundle = _bundle()

    assert bundle.current_for(
        issue_revision="issue-v1", base_revision="base-v1", head_revision="head-v1"
    )
    assert not bundle.current_for(
        issue_revision="issue-v2", base_revision="base-v1", head_revision="head-v1"
    )
    assert not bundle.current_for(
        issue_revision="issue-v1", base_revision="base-v2", head_revision="head-v1"
    )
    assert not bundle.current_for(
        issue_revision="issue-v1", base_revision="base-v1", head_revision="head-v2"
    )


def test_merge_ready_evidence_requires_implementation_and_verification_artifacts() -> None:
    with pytest.raises(InvalidInputError, match="requires implementation"):
        Stage1EvidenceBundle(
            issue_revision="issue-v1",
            base_revision="base-v1",
            head_revision="head-v1",
            pull_request_number=1,
            implementation_artifacts=(),
            verification_artifacts=(),
            outcome=PullRequestOutcome.MERGE_READY,
            observed_at=datetime.now(UTC),
        )


def test_evidence_digest_changes_when_subject_head_changes() -> None:
    first = _bundle()
    second = Stage1EvidenceBundle(
        issue_revision=first.issue_revision,
        base_revision=first.base_revision,
        head_revision="head-v2",
        pull_request_number=first.pull_request_number,
        implementation_artifacts=first.implementation_artifacts,
        verification_artifacts=first.verification_artifacts,
        outcome=first.outcome,
        observed_at=first.observed_at,
    )

    assert first.digest != second.digest
