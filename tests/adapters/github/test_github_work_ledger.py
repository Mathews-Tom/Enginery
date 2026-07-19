from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.work_ports import LifecycleProjection
from enginery.domain.errors import (
    AuthenticationFailureError,
    ExternalConflictError,
    InvalidInputError,
    RateLimitError,
)
from enginery.domain.ids import OperationId, RunId
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


def _issue(*, body: str | None = "Add provider smoke coverage") -> dict[str, object]:
    return {
        "number": 7,
        "title": "Add provider smoke coverage",
        "body": body,
        "updated_at": "2026-07-19T09:00:00Z",
        "url": "https://api.github.com/repos/Mathews-Tom/enginery-provider-smoke/issues/7",
    }


def test_fetch_normalizes_a_revisioned_github_issue() -> None:
    responses: list[object] = [_issue()]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))

    snapshot = ledger.fetch("Mathews-Tom/enginery-provider-smoke#7")

    assert snapshot.work_item.external_reference == "Mathews-Tom/enginery-provider-smoke#7"
    assert snapshot.work_item.objective == "Add provider smoke coverage"
    assert snapshot.work_item.acceptance_criteria == ("Add provider smoke coverage",)
    assert snapshot.source_revision.startswith("2026-07-19T09:00:00Z:sha256:")
    assert calls[0][-1] == "repos/Mathews-Tom/enginery-provider-smoke/issues/7"


def test_fetch_uses_title_when_an_issue_has_no_body() -> None:
    responses: list[object] = [_issue(body=None)]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))

    snapshot = ledger.fetch("Mathews-Tom/enginery-provider-smoke#7")

    assert snapshot.work_item.objective == "Add provider smoke coverage"


def test_fetch_rejects_pull_requests_from_issue_endpoint() -> None:
    payload = _issue()
    payload["pull_request"] = {"url": "https://api.github.com/repos/example/pulls/7"}
    responses: list[object] = [payload]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))

    with pytest.raises(InvalidInputError, match="pull requests"):
        ledger.fetch("Mathews-Tom/enginery-provider-smoke#7")


def test_lifecycle_projection_adopts_matching_comment_before_mutation() -> None:
    operation_id = OperationId("project-lifecycle-1")
    marker = "<!-- enginery:lifecycle:project-lifecycle-1 -->"
    responses: list[object] = [[{"body": marker}]]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))
    projection = LifecycleProjection(
        run_id=RunId("run-1"),
        external_reference="Mathews-Tom/enginery-provider-smoke#7",
        state="active",
        evidence_digest=None,
    )

    result = ledger.publish_lifecycle(projection, operation_id=operation_id)

    assert result is ReconciliationResult.FOUND_MATCHING
    assert len(calls) == 1
    assert calls[0][-1].endswith("page=1")


def test_lifecycle_projection_posts_a_deterministic_marker() -> None:
    responses: list[object] = [[], {"id": 100}]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))
    projection = LifecycleProjection(
        run_id=RunId("run-1"),
        external_reference="Mathews-Tom/enginery-provider-smoke#7",
        state="active",
        evidence_digest=None,
    )

    result = ledger.publish_lifecycle(projection, operation_id=OperationId("project-lifecycle-2"))

    assert result is ReconciliationResult.FOUND_MATCHING
    assert calls[1][1:4] == ("api", "--method", "POST")
    assert "<!-- enginery:lifecycle:project-lifecycle-2 -->" in calls[1][-1]


def test_reconcile_paginates_and_adopts_one_remote_comment() -> None:
    operation_id = OperationId("project-lifecycle-3")
    first_page = [{"body": "unrelated"} for _ in range(100)]
    second_page = [{"body": "<!-- enginery:lifecycle:project-lifecycle-3 -->"}]
    responses: list[object] = [first_page, second_page]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))

    result = ledger.reconcile(operation_id=operation_id)

    assert result is ReconciliationResult.FOUND_MATCHING
    assert calls[0][-1].endswith("page=1")
    assert calls[1][-1].endswith("page=2")


def test_reconcile_marks_duplicate_remote_comments_conflicting() -> None:
    marker = "<!-- enginery:lifecycle:project-lifecycle-4 -->"
    responses: list[object] = [[{"body": marker}, {"body": marker}]]
    calls: list[tuple[str, ...]] = []
    ledger = GitHubWorkLedger(_config(), command_runner=_runner(responses, calls))

    result = ledger.reconcile(operation_id=OperationId("project-lifecycle-4"))

    assert result is ReconciliationResult.FOUND_CONFLICTING


@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        ("HTTP 401: Bad credentials", AuthenticationFailureError),
        ("HTTP 429: API rate limit exceeded", RateLimitError),
        ("HTTP 422: Validation failed", ExternalConflictError),
    ],
)
def test_fetch_classifies_github_failures(stderr: str, error_type: type[Exception]) -> None:
    calls: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return _completed(arguments, {}, returncode=1, stderr=stderr)

    ledger = GitHubWorkLedger(_config(), command_runner=run)

    with pytest.raises(error_type):
        ledger.fetch("Mathews-Tom/enginery-provider-smoke#7")


def test_probe_reports_cli_and_api_capabilities() -> None:
    calls: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[1:] == ("--version",):
            return subprocess.CompletedProcess(
                arguments, 0, stdout="gh version 2.96.0 (2026-07-02)\n", stderr=""
            )
        return _completed(arguments, {"login": "Mathews-Tom"})

    status = GitHubWorkLedger(_config(), command_runner=run).probe()

    assert status.availability.value == "available"
    assert status.fingerprint is not None
    assert status.fingerprint.provider_version.startswith("gh version 2.96.0")
    assert {capability.name for capability in status.fingerprint.capabilities} == {
        "issue_snapshots",
        "lifecycle_projection",
        "pagination",
    }


def test_probe_reports_unavailable_for_empty_cli_version() -> None:
    calls: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    status = GitHubWorkLedger(_config(), command_runner=run).probe()

    assert status.availability.value == "unavailable"
    assert status.fingerprint is None


def test_smoke_repository_must_match_static_allowlist() -> None:
    allowed = _config()
    allowed.require_smoke_repository()
    disallowed = GitHubAdapterConfig(
        repository="Mathews-Tom/Enginery",
        credential_reference="github-keyring:default",
    )

    with pytest.raises(InvalidInputError, match="allowlisted"):
        disallowed.require_smoke_repository()
