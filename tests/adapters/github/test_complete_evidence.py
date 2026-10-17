from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubPullRequests
from enginery.application.work_ports import PullRequestEvidence
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    MissingPrerequisiteError,
    StaleEvidenceError,
    TransientProviderFailureError,
)
from enginery.ledger.artifact_store import ArtifactStore
from enginery.workflows.pull_request import (
    PullRequestOutcome,
    PullRequestRequirements,
    evaluate_pull_request,
)

_HEAD = "a" * 40
_BASE = "b" * 40
_TIME = "2026-10-17T10:00:00Z"


def _pull() -> dict[str, object]:
    return {
        "number": 11,
        "html_url": "https://github.com/owner/project/pull/11",
        "state": "open",
        "head": {"ref": "feature", "sha": _HEAD},
        "base": {"ref": "main", "sha": _BASE},
        "mergeable": True,
    }


def _check(identity: int = 1, *, conclusion: object = "success") -> dict[str, object]:
    return {
        "id": identity,
        "name": "CI",
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": _HEAD,
    }


def _review(
    identity: int = 1,
    *,
    state: str = "APPROVED",
    reviewer: str = "alice",
    submitted_at: str = _TIME,
    head: str = _HEAD,
) -> dict[str, object]:
    return {
        "id": identity,
        "user": {"login": reviewer},
        "state": state,
        "commit_id": head,
        "submitted_at": submitted_at,
    }


def _status(identity: int = 1, *, state: str = "success") -> dict[str, object]:
    return {
        "id": identity,
        "state": state,
        "context": "legacy-ci",
        "created_at": _TIME,
    }


def _combined(statuses: list[object], *, state: str = "success") -> dict[str, object]:
    return {"sha": _HEAD, "state": state, "total_count": len(statuses), "statuses": statuses}


def _responses(
    *,
    reviews: list[object] | None = None,
    checks: list[object] | None = None,
    statuses: list[object] | None = None,
    combined_state: str = "success",
) -> list[object]:
    statuses = [] if statuses is None else statuses
    checks = [_check()] if checks is None else checks
    return [
        _pull(),
        [_review()] if reviews is None else reviews,
        {"total_count": len(checks), "check_runs": checks},
        _combined(statuses, state=combined_state),
        statuses,
        _pull(),
    ]


def _adapter(
    tmp_path: Path,
    responses: list[object],
    *,
    cap: int = 100,
) -> tuple[GitHubPullRequests, ArtifactStore, list[tuple[str, ...]], list[bytes]]:
    calls: list[tuple[str, ...]] = []
    bodies: list[bytes] = []

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        response = responses.pop(0)
        body = response if isinstance(response, str) else json.dumps(response)
        bodies.append(body.encode("utf-8"))
        return subprocess.CompletedProcess(arguments, 0, body, "")

    store = ArtifactStore(tmp_path / "artifacts")
    return (
        GitHubPullRequests(
            GitHubAdapterConfig("owner/project", "keyring:github"),
            run,
            store,
            cap,
        ),
        store,
        calls,
        bodies,
    )


def _outcome(evidence: PullRequestEvidence, *, check: str = "CI") -> PullRequestOutcome:
    return evaluate_pull_request(evidence, PullRequestRequirements(_HEAD, (check,), True))


def test_retains_original_raw_bytes_and_collection_proofs_without_writes(tmp_path: Path) -> None:
    responses = _responses(statuses=[_status()])
    responses[0] = "\r\n " + json.dumps(_pull(), indent=2) + "\r\n"
    adapter, store, calls, bodies = _adapter(tmp_path, responses)

    evidence = adapter.evidence(11)

    assert evidence.complete
    assert _outcome(evidence) is PullRequestOutcome.MERGE_READY
    assert tuple(store.read_bytes(digest) for digest in evidence.raw_artifact_digests) == tuple(
        bodies
    )
    assert evidence.raw_artifact_digests[0] == Digest.of_bytes(bodies[0])
    assert evidence.completeness is not None
    assert {proof.collection for proof in evidence.completeness} == {
        "reviews",
        "check_runs",
        "combined_status",
        "statuses",
    }
    assert all(proof.proven and proof.page_count == 1 for proof in evidence.completeness)
    assert all(arguments[1:4] == ("api", "--method", "GET") for arguments in calls)
    assert all("requested_reviewers" not in arguments[-1] for arguments in calls)
    assert all("comments" not in arguments[-1] for arguments in calls)


def test_failing_check_after_first_full_page_blocks(tmp_path: Path) -> None:
    first_page = [_check(identity) for identity in range(1, 101)]
    responses = _responses()
    responses[2:3] = [
        {"total_count": 101, "check_runs": first_page},
        {"total_count": 101, "check_runs": [_check(101, conclusion="failure")]},
    ]
    adapter, _, calls, _ = _adapter(tmp_path, responses)

    evidence = adapter.evidence(11)

    assert evidence.complete
    assert len(evidence.checks) == 101
    assert _outcome(evidence) is PullRequestOutcome.BLOCKED
    assert calls[3][-1].endswith("/check-runs?per_page=100&page=2")


@pytest.mark.parametrize("state", ["failure", "error"])
def test_failing_commit_status_vetoes_successful_checks(tmp_path: Path, state: str) -> None:
    adapter, _, _, _ = _adapter(
        tmp_path,
        _responses(statuses=[_status(state=state)], combined_state=state),
    )
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.BLOCKED


def test_required_check_can_be_a_commit_status_context(tmp_path: Path) -> None:
    adapter, _, _, _ = _adapter(tmp_path, _responses(checks=[], statuses=[_status()]))
    assert _outcome(adapter.evidence(11), check="legacy-ci") is PullRequestOutcome.MERGE_READY


def test_historical_failure_does_not_override_newer_status(tmp_path: Path) -> None:
    responses = _responses(statuses=[_status(2)])
    responses[4] = [_status(1, state="failure"), _status(2)]
    adapter, _, _, _ = _adapter(tmp_path, responses)
    evidence = adapter.evidence(11)
    assert evidence.statuses[0].status_id == 2
    assert _outcome(evidence) is PullRequestOutcome.MERGE_READY


def test_combined_and_per_context_disagreement_holds(tmp_path: Path) -> None:
    responses = _responses(statuses=[_status()])
    responses[4] = [_status(state="failure")]
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(StaleEvidenceError, match="disagree"):
        adapter.evidence(11)


@pytest.mark.parametrize("reverse", [False, True])
def test_current_changes_request_vetoes_prior_approval(tmp_path: Path, reverse: bool) -> None:
    reviews = [
        _review(1),
        _review(2, state="CHANGES_REQUESTED", submitted_at="2026-10-17T10:01:00Z"),
        _review(3, reviewer="bob"),
    ]
    if reverse:
        reviews.reverse()
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=reviews))
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.BLOCKED


def test_latest_review_id_breaks_timestamp_tie(tmp_path: Path) -> None:
    reviews = [_review(2, state="CHANGES_REQUESTED"), _review(1)]
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=reviews))
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.BLOCKED


def test_later_approval_replaces_old_changes_request(tmp_path: Path) -> None:
    reviews = [
        _review(2, submitted_at="2026-10-17T10:01:00Z"),
        _review(1, state="CHANGES_REQUESTED"),
    ]
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=reviews))
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.MERGE_READY


@pytest.mark.parametrize("state", ["COMMENTED", "PENDING"])
@pytest.mark.parametrize(
    "opinion,outcome",
    [
        ("CHANGES_REQUESTED", PullRequestOutcome.BLOCKED),
        ("APPROVED", PullRequestOutcome.MERGE_READY),
    ],
)
def test_non_opinion_review_preserves_latest_opinion(
    tmp_path: Path, state: str, opinion: str, outcome: PullRequestOutcome
) -> None:
    later = _review(2, state=state, submitted_at="2026-10-17T10:01:00Z")
    if state == "PENDING":
        later["submitted_at"] = None
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=[_review(state=opinion), later]))
    assert _outcome(adapter.evidence(11)) is outcome


@pytest.mark.parametrize("computed", [True, False])
def test_mergeability_computed_during_collection_is_used(tmp_path: Path, computed: bool) -> None:
    responses = _responses()
    initial = _pull()
    initial["mergeable"] = None
    terminal = _pull()
    terminal["mergeable"] = computed
    responses[0] = initial
    responses[-1] = terminal
    adapter, _, _, _ = _adapter(tmp_path, responses)
    assert adapter.evidence(11).mergeable is computed


def test_new_head_does_not_reuse_old_approval(tmp_path: Path) -> None:
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=[_review(head="c" * 40)]))
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.WAITING


@pytest.mark.parametrize("collection", ["reviews", "check_runs", "combined_status", "statuses"])
def test_terminal_page_at_cap_is_not_a_complete_prefix(tmp_path: Path, collection: str) -> None:
    responses = _responses()
    if collection == "reviews":
        responses[1:2] = [[_review(identity) for identity in range(1, 101)], []]
    elif collection == "check_runs":
        responses[2:3] = [
            {"total_count": 100, "check_runs": [_check(i) for i in range(1, 101)]},
            {"total_count": 100, "check_runs": []},
        ]
    elif collection == "combined_status":
        page = [_status(i) for i in range(1, 101)]
        responses[3:4] = [
            _combined(page),
            {"sha": _HEAD, "state": "success", "total_count": 100, "statuses": []},
        ]
    else:
        responses[4:5] = [[_status(i) for i in range(1, 101)], []]
    adapter, _, _, _ = _adapter(tmp_path, responses, cap=2)
    with pytest.raises(TransientProviderFailureError, match="cap reached"):
        adapter.evidence(11)


@pytest.mark.parametrize("index,field", [(2, "check_runs"), (3, "statuses")])
def test_short_page_must_agree_with_total_count(tmp_path: Path, index: int, field: str) -> None:
    responses = _responses()
    responses[index] = {"sha": _HEAD, "state": "success", "total_count": 2, field: []}
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(TransientProviderFailureError, match="short page"):
        adapter.evidence(11)


def test_total_count_change_across_pages_holds(tmp_path: Path) -> None:
    responses = _responses()
    responses[2:3] = [
        {"total_count": 101, "check_runs": [_check(i) for i in range(1, 101)]},
        {"total_count": 100, "check_runs": []},
    ]
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(StaleEvidenceError, match="total_count changed"):
        adapter.evidence(11)


@pytest.mark.parametrize("index,field", [(1, None), (2, "check_runs"), (3, "statuses"), (4, None)])
def test_malformed_element_anywhere_fails_closed(
    tmp_path: Path,
    index: int,
    field: str | None,
) -> None:
    responses = _responses()
    if field is None:
        responses[index] = [None]
    else:
        responses[index] = {"sha": _HEAD, "state": "success", "total_count": 1, field: [None]}
    adapter, store, _, bodies = _adapter(tmp_path, responses)
    with pytest.raises(TransientProviderFailureError, match="element must be an object"):
        adapter.evidence(11)
    assert store.read_bytes(Digest.of_bytes(bodies[-1])) == bodies[-1]


@pytest.mark.parametrize(
    "review",
    [
        {"id": 1, "user": {}, "state": "APPROVED", "commit_id": _HEAD, "submitted_at": _TIME},
        _review(submitted_at="not-a-time"),
        _review(state="UNKNOWN"),
    ],
)
def test_malformed_review_fields_hold(tmp_path: Path, review: dict[str, object]) -> None:
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=[review]))
    with pytest.raises(TransientProviderFailureError):
        adapter.evidence(11)


def test_malformed_check_conclusion_holds(tmp_path: Path) -> None:
    adapter, _, _, _ = _adapter(tmp_path, _responses(checks=[_check(conclusion={})]))
    with pytest.raises(TransientProviderFailureError, match="conclusion"):
        adapter.evidence(11)


def test_stale_combined_status_head_holds(tmp_path: Path) -> None:
    responses = _responses()
    responses[3] = {"sha": "c" * 40, "state": "success", "total_count": 0, "statuses": []}
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(StaleEvidenceError, match="stale head"):
        adapter.evidence(11)


def test_head_changed_during_collection_holds(tmp_path: Path) -> None:
    responses = _responses()
    last = _pull()
    last["head"] = {"ref": "feature", "sha": "c" * 40}
    responses[-1] = last
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(StaleEvidenceError, match="changed while"):
        adapter.evidence(11)


def test_incomplete_proof_cannot_authorize(tmp_path: Path) -> None:
    adapter, _, _, _ = _adapter(tmp_path, _responses())
    evidence = adapter.evidence(11)
    assert evidence.completeness is not None
    proof, *others = evidence.completeness
    incomplete = replace(evidence, completeness=(replace(proof, complete=False), *others))
    assert _outcome(incomplete) is PullRequestOutcome.BLOCKED


def test_missing_store_never_fabricates_live_completeness() -> None:
    adapter = GitHubPullRequests(GitHubAdapterConfig("owner/project", "keyring:github"))
    with pytest.raises(MissingPrerequisiteError, match="artifact store"):
        adapter.evidence(11)


def test_oversized_evidence_is_refused_without_retaining_prefix(tmp_path: Path) -> None:
    adapter, store, _, _ = _adapter(tmp_path, [" " * (1024 * 1024 + 1)])
    with pytest.raises(TransientProviderFailureError, match="1 MiB"):
        adapter.evidence(11)
    assert tuple(store.iter_digests()) == ()


def test_pending_status_is_not_merge_ready(tmp_path: Path) -> None:
    adapter, _, _, _ = _adapter(
        tmp_path,
        _responses(statuses=[_status(state="pending")], combined_state="pending"),
    )
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.WAITING


def test_late_status_context_failure_is_not_a_successful_prefix(tmp_path: Path) -> None:
    statuses = [
        {**_status(identity), "context": f"context-{identity}"} for identity in range(1, 102)
    ]
    statuses[-1]["state"] = "failure"
    responses = _responses()
    responses[3:5] = [
        {"sha": _HEAD, "state": "failure", "total_count": 101, "statuses": statuses[:100]},
        {"sha": _HEAD, "state": "failure", "total_count": 101, "statuses": statuses[100:]},
        statuses[:100],
        statuses[100:],
    ]
    adapter, _, _, _ = _adapter(tmp_path, responses)
    evidence = adapter.evidence(11)
    assert len(evidence.statuses) == 101
    assert _outcome(evidence) is PullRequestOutcome.BLOCKED


def test_invalid_json_is_retained_but_never_decoded_as_evidence(tmp_path: Path) -> None:
    adapter, store, _, bodies = _adapter(tmp_path, ['{"number":'])
    with pytest.raises(TransientProviderFailureError, match="invalid JSON"):
        adapter.evidence(11)
    assert store.read_bytes(Digest.of_bytes(bodies[0])) == bodies[0]


def test_no_count_is_not_a_live_check_completeness_proof(tmp_path: Path) -> None:
    responses = _responses()
    responses[2] = {"check_runs": [_check()]}
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(TransientProviderFailureError, match="total_count"):
        adapter.evidence(11)


def test_malformed_historical_status_cannot_be_hidden_by_current_success(tmp_path: Path) -> None:
    responses = _responses(statuses=[_status(2)])
    responses[4] = [_status(2), {**_status(1), "created_at": "invalid"}]
    adapter, _, _, _ = _adapter(tmp_path, responses)
    with pytest.raises(TransientProviderFailureError, match="timestamp"):
        adapter.evidence(11)


def test_latest_review_timestamp_precedes_identifier_tiebreak(tmp_path: Path) -> None:
    reviews = [
        _review(20),
        _review(10, state="CHANGES_REQUESTED", submitted_at="2026-10-17T10:01:00Z"),
    ]
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=reviews))
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.BLOCKED


def test_dismissed_review_does_not_resurrect_prior_approval(tmp_path: Path) -> None:
    reviews = [
        _review(1),
        _review(2, state="DISMISSED", submitted_at="2026-10-17T10:01:00Z"),
    ]
    adapter, _, _, _ = _adapter(tmp_path, _responses(reviews=reviews))
    assert _outcome(adapter.evidence(11)) is PullRequestOutcome.WAITING
