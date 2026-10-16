from __future__ import annotations

import json
import subprocess

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.factory_intake import IssueCandidate, declared_classification
from enginery.domain.errors import InvalidInputError, TransientProviderFailureError


def _candidate(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "updated_at": "2026-10-16T00:00:00Z",
        "labels": [{"name": "enginery/work-kind/issue"}, {"name": "enginery/risk/low"}],
    }


def _ledger(pages: list[object], calls: list[tuple[str, ...]]) -> GitHubWorkLedger:
    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(pages.pop(0)), "")

    return GitHubWorkLedger(GitHubAdapterConfig("o/r", "github:default"), runner)


def test_listing_paginates_by_raw_page_size_excludes_prs_and_uses_listing_labels() -> None:
    first_page = [_candidate(number) for number in range(1, 100)]
    first_page.append({"pull_request": {}, "number": 100})
    calls: list[tuple[str, ...]] = []
    ledger = _ledger([first_page, [_candidate(101)]], calls)

    candidates = ledger.list_candidates(since="2026-10-15T00:00:00Z")

    assert len(candidates) == 100
    assert candidates[-1] == IssueCandidate(
        "o/r#101",
        "Issue 101",
        ("enginery/risk/low", "enginery/work-kind/issue"),
        "2026-10-16T00:00:00Z",
    )
    assert "o/r#100" not in {candidate.external_reference for candidate in candidates}
    assert [call[-1] for call in calls] == [
        "repos/o/r/issues?state=open&per_page=100&page=1&since=2026-10-15T00%3A00%3A00Z",
        "repos/o/r/issues?state=open&per_page=100&page=2&since=2026-10-15T00%3A00%3A00Z",
    ]
    assert ledger.github_reads == 2
    assert declared_classification(candidates[0].labels)[0].value == "issue"


def test_short_page_stops_without_extra_request_and_keeps_ineligible_issues() -> None:
    candidate = _candidate(1)
    candidate["labels"] = [{"name": "bug"}]
    calls: list[tuple[str, ...]] = []
    ledger = _ledger([[candidate]], calls)
    candidates = ledger.list_candidates(since=None)
    assert len(calls) == 1
    assert calls[0][-1] == "repos/o/r/issues?state=open&per_page=100&page=1"
    assert candidates[0].labels == ("bug",)
    with pytest.raises(InvalidInputError, match="exactly one declared work-kind"):
        declared_classification(candidates[0].labels)


def test_empty_listing_is_empty_and_records_one_read() -> None:
    ledger = _ledger([[]], [])
    assert ledger.list_candidates(since=None) == ()
    assert ledger.github_reads == 1


def test_full_pr_only_page_still_fetches_following_issue_page() -> None:
    ledger = _ledger([[{"pull_request": {}} for _ in range(100)], [_candidate(101)]], [])
    assert [issue.external_reference for issue in ledger.list_candidates(since=None)] == ["o/r#101"]
    assert ledger.github_reads == 2


@pytest.mark.parametrize("record", [None, {"number": 1, "labels": "bug"}])
def test_malformed_listing_fails_without_silently_omitting_an_issue(record: object) -> None:
    ledger = _ledger([[record]], [])
    with pytest.raises(TransientProviderFailureError):
        ledger.list_candidates(since=None)
    assert ledger.github_reads == 1
