from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubWorkLedger,
    verify_g4_evidence_pull_request,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    InvalidInputError,
    StaleEvidenceError,
    TransientProviderFailureError,
)
from enginery.domain.g4_deficiency import G4DeficiencyFinding

_BODY = "# Recurring validation failure\n"
_HEAD = "a" * 40


def _finding() -> G4DeficiencyFinding:
    return G4DeficiencyFinding(
        finding_id="finding-1",
        deficiency="Validation command fails after generated dependency update.",
        cited_run_ids=("run-1", "run-2"),
        evidence_pull_request_number=42,
        evidence_document_digest=Digest.of_bytes(_BODY.encode()),
        producer_principal_id="producer",
        evidence_pull_request_author_login="evidence-author",
        recorded_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def _pull_request() -> dict[str, object]:
    return {
        "number": 42,
        "merged": True,
        "body": _BODY,
        "head": {"sha": _HEAD},
        "user": {"id": 99, "login": "evidence-author"},
    }


def _reviews(*, head: str = _HEAD) -> list[object]:
    return [
        {
            "user": {"id": 101, "login": "approver-one"},
            "state": "APPROVED",
            "commit_id": head,
        },
        {
            "user": {"id": 102, "login": "approver-two"},
            "state": "APPROVED",
            "commit_id": head,
        },
    ]


def _principals() -> dict[str, int]:
    return {"producer": 100, "one": 101, "two": 102}


def test_verifier_requires_exact_merged_head_approvals() -> None:
    evidence = verify_g4_evidence_pull_request(
        finding=_finding(),
        principal_github_user_ids=_principals(),
        pull_request=_pull_request(),
        reviews=_reviews(),
        verified_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert evidence.approver_principal_ids == ("one", "two")
    assert evidence.producer_github_user_id == 100
    assert evidence.evidence_pull_request_author_github_user_id == 99
    assert evidence.approver_github_user_ids == (101, 102)
    assert evidence.merged_head_revision == _HEAD


def test_verifier_rejects_stale_approval() -> None:
    with pytest.raises(StaleEvidenceError, match="two distinct current"):
        verify_g4_evidence_pull_request(
            finding=_finding(),
            principal_github_user_ids=_principals(),
            pull_request=_pull_request(),
            reviews=_reviews(head="b" * 40),
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_verifier_rejects_unmerged_evidence_pull_request() -> None:
    pull_request = _pull_request()
    pull_request["merged"] = False

    with pytest.raises(StaleEvidenceError, match="not merged"):
        verify_g4_evidence_pull_request(
            finding=_finding(),
            principal_github_user_ids=_principals(),
            pull_request=pull_request,
            reviews=_reviews(),
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_verifier_rejects_duplicate_approvals_from_one_principal() -> None:
    reviews = [
        {"user": {"id": 101, "login": "approver-one"}, "state": "APPROVED", "commit_id": _HEAD},
        {"user": {"id": 101, "login": "approver-one"}, "state": "APPROVED", "commit_id": _HEAD},
    ]

    with pytest.raises(StaleEvidenceError, match="two distinct current"):
        verify_g4_evidence_pull_request(
            finding=_finding(),
            principal_github_user_ids=_principals(),
            pull_request=_pull_request(),
            reviews=reviews,
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_verifier_rejects_document_digest_mismatch() -> None:
    pull_request = _pull_request()
    pull_request["body"] = "different evidence"
    with pytest.raises(StaleEvidenceError, match="document digest"):
        verify_g4_evidence_pull_request(
            finding=_finding(),
            principal_github_user_ids=_principals(),
            pull_request=pull_request,
            reviews=_reviews(),
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_verifier_rejects_evidence_author_who_is_the_finding_producer() -> None:
    pull_request = _pull_request()
    pull_request["user"] = {"id": 100, "login": "producer-login"}
    finding = replace(_finding(), evidence_pull_request_author_login="producer-login")

    with pytest.raises(InvalidInputError, match="author cannot be"):
        verify_g4_evidence_pull_request(
            finding=finding,
            principal_github_user_ids=_principals(),
            pull_request=pull_request,
            reviews=_reviews(),
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_verifier_rejects_reused_login_with_wrong_github_user_id() -> None:
    reviews = _reviews()
    for review in reviews:
        reviewer = review["user"]
        assert isinstance(reviewer, dict)
        reviewer["id"] = int(reviewer["id"]) + 1000

    with pytest.raises(StaleEvidenceError, match="two distinct current"):
        verify_g4_evidence_pull_request(
            finding=_finding(),
            principal_github_user_ids=_principals(),
            pull_request=_pull_request(),
            reviews=reviews,
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_verifier_rejects_review_without_numeric_github_user_id() -> None:
    reviews = _reviews()
    reviewer = reviews[0]["user"]
    assert isinstance(reviewer, dict)
    reviewer.pop("id")

    with pytest.raises(TransientProviderFailureError, match="positive integer"):
        verify_g4_evidence_pull_request(
            finding=_finding(),
            principal_github_user_ids=_principals(),
            pull_request=_pull_request(),
            reviews=reviews,
            verified_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


def test_reader_fetches_every_review_page_before_verifying() -> None:
    calls: list[tuple[str, ...]] = []
    first_page = [
        {"user": {"id": 9, "login": "other"}, "state": "COMMENTED", "commit_id": _HEAD}
        for _ in range(100)
    ]
    responses: list[object] = [_pull_request(), first_page, _reviews()]

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(responses.pop(0)), "")

    evidence = GitHubWorkLedger(
        GitHubAdapterConfig(
            repository="Mathews-Tom/Enginery",
            credential_reference="test-credential",
        ),
        command_runner=run,
    ).verify_g4_evidence(
        finding=_finding(),
        principal_github_user_ids=_principals(),
        verified_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert evidence.approver_principal_ids == ("one", "two")
    assert calls[1][-1].endswith("/reviews?per_page=100&page=1")
    assert calls[2][-1].endswith("/reviews?per_page=100&page=2")
