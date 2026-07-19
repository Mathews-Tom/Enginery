from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubPullRequests
from enginery.application.work_ports import PullRequestRequest
from enginery.domain.errors import AmbiguousExternalSideEffectError, TransientProviderFailureError
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult


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


def _request(operation_id: str = "pull-request-1") -> PullRequestRequest:
    return PullRequestRequest(
        head_branch="enginery/run-1",
        base_branch="main",
        title="Add GitHub provider",
        body="Provider implementation",
        operation_id=OperationId(operation_id),
    )


def _pull(*, marker: str, number: int = 11) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/Mathews-Tom/enginery-provider-smoke/pull/{number}",
        "state": "open",
        "body": marker,
        "head": {"ref": "enginery/run-1", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
    }


def test_create_opens_marked_pull_request_when_none_exists() -> None:
    responses: list[object] = [[], _pull(marker="<!-- enginery:pull-request:pull-request-1 -->")]
    calls: list[tuple[str, ...]] = []
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    snapshot = adapter.create_or_update(_request())

    assert snapshot.number == 11
    assert calls[0][-1].endswith("page=1")
    assert calls[1][1:4] == ("api", "--method", "POST")
    assert "<!-- enginery:pull-request:pull-request-1 -->" in calls[1][-1]


def test_create_or_update_adopts_and_updates_one_matching_pull_request() -> None:
    marker = "<!-- enginery:pull-request:pull-request-1 -->"
    responses: list[object] = [[_pull(marker=marker)], _pull(marker=marker)]
    calls: list[tuple[str, ...]] = []
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    snapshot = adapter.create_or_update(_request())

    assert snapshot.number == 11
    assert calls[1][1:4] == ("api", "--method", "PATCH")
    assert "repos/Mathews-Tom/enginery-provider-smoke/pulls/11" in calls[1]


def test_timeout_after_create_reconciles_to_the_existing_pull_request() -> None:
    marker = "<!-- enginery:pull-request:pull-request-1 -->"
    responses: list[object] = [
        [],
        subprocess.CompletedProcess((), 1, stdout="", stderr="network timeout"),
        [_pull(marker=marker)],
    ]
    calls: list[tuple[str, ...]] = []
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(TransientProviderFailureError):
        adapter.create_or_update(_request())

    assert (
        adapter.reconcile(operation_id=OperationId("pull-request-1"))
        is ReconciliationResult.FOUND_MATCHING
    )
    assert calls[1][1:4] == ("api", "--method", "POST")
    assert calls[2][-1].endswith("page=1")


def test_duplicate_markers_are_ambiguous_and_reconcile_conflicting() -> None:
    marker = "<!-- enginery:pull-request:pull-request-1 -->"
    responses: list[object] = [
        [_pull(marker=marker), _pull(marker=marker, number=12)],
        [_pull(marker=marker), _pull(marker=marker, number=12)],
    ]
    calls: list[tuple[str, ...]] = []
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(AmbiguousExternalSideEffectError):
        adapter.create_or_update(_request())

    assert (
        adapter.reconcile(operation_id=OperationId("pull-request-1"))
        is ReconciliationResult.FOUND_CONFLICTING
    )


def test_get_binds_head_and_base_revisions() -> None:
    marker = "<!-- enginery:pull-request:pull-request-1 -->"
    responses: list[object] = [_pull(marker=marker)]
    calls: list[tuple[str, ...]] = []
    adapter = GitHubPullRequests(_config(), command_runner=_runner(responses, calls))

    snapshot = adapter.get(11)

    assert snapshot.head_revision == "a" * 40
    assert snapshot.base_revision == "b" * 40
    assert calls[0][-1] == "repos/Mathews-Tom/enginery-provider-smoke/pulls/11"
