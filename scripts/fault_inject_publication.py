#!/usr/bin/env python3
"""Fault-inject success-with-timeout at Stage 2's merge and publish boundaries.

Each scenario simulates a provider mutation that actually succeeded but
whose response was lost to the caller (a dropped connection, a client
timeout) -- the exact "ambiguous external side effect" class design.md
names -- then proves the affected adapter's own idempotent-retry design
reaches the correct terminal state without a duplicate external effect.
No scenario here builds a separate reconciliation loop; each adapter is
retried through its own public API exactly as a real caller would retry
it, using the shared fault-injection framework from M3/M5
(``fault_injection.framework``) for scenario registration and reporting.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubPullRequests,
    GitHubReleaseAdapter,
    GitHubReleaseRequest,
)
from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.delivery_ports import PublicationRequest, ReleaseArtifact
from enginery.domain.digests import Digest
from enginery.domain.ids import OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from fault_injection.framework import FaultScenario, main_for

_COMMIT = "c" * 40
_HEAD = "a" * 40


def _config() -> GitHubAdapterConfig:
    return GitHubAdapterConfig(
        repository="Mathews-Tom/enginery-provider-smoke",
        credential_reference="github-keyring:default",
    )


def _pull_payload(*, merged: bool = False) -> dict[str, object]:
    return {
        "number": 11,
        "html_url": "https://github.com/Mathews-Tom/enginery-provider-smoke/pull/11",
        "state": "closed" if merged else "open",
        "head": {"ref": "fixture/m1", "sha": _HEAD},
        "base": {"ref": "main", "sha": "b" * 40},
        "merged": merged,
        "merged_at": "2026-07-21T00:00:00Z" if merged else None,
    }


def _release_payload() -> dict[str, object]:
    return {
        "tag_name": "enginery-stage2-fixture-v0.1.0",
        "target_commitish": _COMMIT,
        "name": "v0.1.0",
        "body": "Initial release.",
        "draft": False,
        "prerelease": False,
    }


def _connection_reset() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        (), 1, stdout="", stderr="curl: (56) Connection reset by peer"
    )


def _ok(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), 0, stdout=json.dumps(payload), stderr="")


def _not_found() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found")


def _expect_raises(action) -> None:  # type: ignore[no-untyped-def]
    try:
        action()
    except Exception:  # BLE001 acceptable here: any exception confirms the ambiguous
        return  # first attempt surfaced as a failure, matching the fault scenario.
    raise AssertionError("expected the ambiguous first attempt to raise")


def scenario_merge_success_with_timeout_reconciles_without_duplicate_merge() -> None:
    calls: list[tuple[str, ...]] = []
    # The server actually merged the pull request on the first attempt --
    # the PUT succeeded server-side -- but the caller's connection was
    # reset before the response arrived, so the first merge() call must
    # surface a failure even though the mutation already landed.
    first_responses: list[subprocess.CompletedProcess[str]] = [
        _ok(_pull_payload(merged=False)),  # pre-flight read
        _connection_reset(),  # the mutation itself, lost in transit
    ]

    def first_runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return first_responses.pop(0)

    adapter = GitHubPullRequests(_config(), command_runner=first_runner)
    _expect_raises(
        lambda: adapter.merge(11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1"))
    )
    assert len(calls) == 2, "expected one pre-flight read and one failed mutation attempt"

    # Retry: the caller re-invokes merge() exactly as it would after any
    # transient failure. The pre-flight read now observes the server's
    # already-applied mutation and adopts it idempotently -- no second
    # mutation attempt, no duplicate merge.
    retry_responses: list[subprocess.CompletedProcess[str]] = [_ok(_pull_payload(merged=True))]

    def retry_runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return retry_responses.pop(0)

    adapter_retry = GitHubPullRequests(_config(), command_runner=retry_runner)
    result = adapter_retry.merge(
        11, expected_head_revision=_HEAD, operation_id=OperationId("merge-1")
    )
    assert result.merged is True
    assert not retry_responses
    assert len(calls) == 3, "the retry must perform only the pre-flight read, never a second PUT"


def scenario_github_release_success_with_timeout_reconciles_without_duplicate_release() -> None:
    calls: list[tuple[str, ...]] = []
    # The release was actually created server-side, but the caller never
    # observed the 201 response.
    responses: list[subprocess.CompletedProcess[str]] = [
        _not_found(),  # pre-flight tag existence check: no release yet
        _connection_reset(),  # the create POST, lost in transit
    ]

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return responses.pop(0)

    artifact = ReleaseArtifact(
        version="0.1.0", digest=Digest.of_bytes(b"wheel"), media_type="application/vnd.pypa.wheel"
    )
    request = PublicationRequest(
        run_id=RunId("run-1"),
        artifact=artifact,
        destination="github-release",
        operation_id=OperationId("pub-1"),
    )
    adapter = GitHubReleaseAdapter(_config(), command_runner=runner)
    adapter.stage(
        artifact.digest,
        GitHubReleaseRequest(
            tag_name="enginery-stage2-fixture-v0.1.0",
            target_commitish=_COMMIT,
            name="v0.1.0",
            body="Initial release.",
        ),
    )
    _expect_raises(lambda: adapter.publish(request))
    assert len(calls) == 2

    # Reconcile: the operation_id was recorded before the ambiguous
    # attempt, so reconcile() alone -- without calling publish() again --
    # can already answer whether the mutation landed.
    reconcile_responses: list[subprocess.CompletedProcess[str]] = [_ok(_release_payload())]

    def reconcile_runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return reconcile_responses.pop(0)

    adapter.command_runner = reconcile_runner
    outcome = adapter.reconcile(operation_id=OperationId("pub-1"))
    assert outcome is ReconciliationResult.FOUND_MATCHING
    assert len(calls) == 3


def scenario_pypi_publish_success_with_timeout_recovers_via_retry() -> None:
    # uv publish's own documented behavior: a retried upload with
    # --check-url skips files already present at the destination rather
    # than re-uploading or erroring. The first attempt fails at the
    # transport layer even though the file reached the index; the second
    # (identical) invocation succeeds because uv itself detects the
    # existing, identical file.
    attempts: list[subprocess.CompletedProcess[str]] = [
        _connection_reset(),
        subprocess.CompletedProcess((), 0, stdout="", stderr=""),
    ]

    def runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        del command, cwd
        return attempts.pop(0)

    config = PyPiAdapterConfig(
        project_name="enginery-stage2-fixture",
        index_url="https://test.pypi.org/simple/",
        publish_url="https://test.pypi.org/legacy/",
        json_api_base="https://test.pypi.org/pypi",
    )
    artifact = ReleaseArtifact(
        version="0.1.0", digest=Digest.of_bytes(b"wheel"), media_type="application/vnd.pypa.wheel"
    )
    request = PublicationRequest(
        run_id=RunId("run-1"),
        artifact=artifact,
        destination="pypi",
        operation_id=OperationId("pub-2"),
    )
    adapter = PyPiAdapter(config, command_runner=runner)
    wheel_path = Path("/tmp/enginery-stage2-fixture-fault-inject.whl")
    wheel_path.write_bytes(b"wheel")
    try:
        adapter.stage(wheel_path)
        _expect_raises(lambda: adapter.publish(request))
        receipt = adapter.publish(request)
        assert receipt.version == "0.1.0"
        assert not attempts
    finally:
        wheel_path.unlink(missing_ok=True)


SCENARIOS = (
    FaultScenario(
        name="merge_success_with_timeout",
        description=(
            "A merge that succeeds server-side but times out client-side is adopted "
            "idempotently on retry, never duplicated."
        ),
        run=scenario_merge_success_with_timeout_reconciles_without_duplicate_merge,
    ),
    FaultScenario(
        name="github_release_success_with_timeout",
        description=(
            "A GitHub Release created server-side but lost client-side is discovered "
            "by reconcile() without a duplicate release."
        ),
        run=scenario_github_release_success_with_timeout_reconciles_without_duplicate_release,
    ),
    FaultScenario(
        name="pypi_publish_success_with_timeout",
        description=(
            "A PyPI publish that fails client-side after the file reached the index "
            "recovers cleanly on retry via uv's own check-url idempotency."
        ),
        run=scenario_pypi_publish_success_with_timeout_recovers_via_retry,
    ),
)


if __name__ == "__main__":
    raise SystemExit(main_for(SCENARIOS))
