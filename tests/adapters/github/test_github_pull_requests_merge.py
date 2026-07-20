from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubPullRequests
from enginery.domain.errors import (
    ExternalConflictError,
    InvalidInputError,
    StaleEvidenceError,
)
from enginery.domain.ids import OperationId

_HEAD = "a" * 40
_OTHER_HEAD = "c" * 40


def _config() -> GitHubAdapterConfig:
    return GitHubAdapterConfig(
        repository="Mathews-Tom/enginery-provider-smoke",
        credential_reference="github-keyring:default",
    )


def _completed(
    arguments: tuple[str, ...], payload: object, *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout=json.dumps(payload) if returncode == 0 else "",
        stderr=stderr,
    )


def _runner(
    responses: list[object], calls: list[tuple[str, ...]]
) -> Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]:
    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        response = responses.pop(0)
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return _completed(arguments, response)

    return run


def _pull(
    *, number: int = 11, head: str = _HEAD, state: str = "open", merged: bool = False
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/Mathews-Tom/enginery-provider-smoke/pull/{number}",
        "state": state,
        "head": {"ref": "enginery/run-1", "sha": head},
        "base": {"ref": "main", "sha": "b" * 40},
        "merged": merged,
        "merged_at": "2026-07-21T00:00:00Z" if merged else None,
    }


def test_merge_calls_put_with_sha_precondition_and_returns_merged_snapshot() -> None:
    calls: list[tuple[str, ...]] = []
    responses: list[object] = [
        _pull(head=_HEAD, state="open", merged=False),
        {"sha": "z" * 40, "merged": True, "message": "merged"},
        _pull(head=_HEAD, state="closed", merged=True),
    ]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    result = adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))

    assert result.merged is True
    assert result.head_revision == _HEAD
    put_call = calls[1]
    assert "PUT" in put_call
    assert "repos/Mathews-Tom/enginery-provider-smoke/pulls/11/merge" in put_call
    assert f"sha={_HEAD}" in put_call
    assert "merge_method=merge" in put_call


def test_merge_rejects_stale_head_before_attempting_mutation() -> None:
    calls: list[tuple[str, ...]] = []
    responses: list[object] = [_pull(head=_OTHER_HEAD, state="open", merged=False)]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(StaleEvidenceError):
        adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))

    assert len(calls) == 1  # only the pre-flight read; no mutation attempted


def test_merge_retry_after_ambiguous_timeout_is_idempotent_at_the_same_head() -> None:
    calls: list[tuple[str, ...]] = []
    responses: list[object] = [_pull(head=_HEAD, state="closed", merged=True)]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    result = adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))

    assert result.merged is True
    assert len(calls) == 1  # no second mutation attempt


def test_merge_raises_conflict_when_already_merged_at_a_different_head() -> None:
    calls: list[tuple[str, ...]] = []
    responses: list[object] = [_pull(head=_OTHER_HEAD, state="closed", merged=True)]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(ExternalConflictError):
        adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))


def test_merge_raises_conflict_on_not_mergeable() -> None:
    calls: list[tuple[str, ...]] = []
    responses: list[object] = [
        _pull(head=_HEAD, state="open", merged=False),
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 405: Method Not Allowed"),
    ]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(ExternalConflictError):
        adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))


def test_merge_raises_conflict_on_sha_precondition_mismatch() -> None:
    calls: list[tuple[str, ...]] = []
    responses: list[object] = [
        _pull(head=_HEAD, state="open", merged=False),
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 409: Conflict"),
    ]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(ExternalConflictError):
        adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))


def test_merge_rejects_unknown_merge_method() -> None:
    adapter = GitHubPullRequests(_config(), command_runner=_runner([], []))

    with pytest.raises(InvalidInputError):
        adapter.merge(
            11,
            expected_head_revision=_HEAD,
            operation_id=OperationId("merge-1"),
            merge_method="fast-forward",
        )


def test_payload_without_merged_field_still_reports_merged_via_merged_at() -> None:
    """The list ("Pull Request Simple") schema omits ``merged`` and only
    carries ``merged_at``; derivation must not silently default to False."""
    calls: list[tuple[str, ...]] = []
    payload = _pull(head=_HEAD, state="closed", merged=True)
    del payload["merged"]
    responses: list[object] = [payload]
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    snapshot = adapter.get(11)

    assert snapshot.merged is True
