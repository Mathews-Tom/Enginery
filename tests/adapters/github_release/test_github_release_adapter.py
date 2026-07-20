from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubReleaseAdapter, GitHubReleaseRequest
from enginery.application.delivery_ports import PublicationRequest, ReleaseArtifact
from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.ids import OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult

_COMMIT = "c" * 40


def _config() -> GitHubAdapterConfig:
    return GitHubAdapterConfig(
        repository="Mathews-Tom/enginery-provider-smoke",
        credential_reference="github-keyring:default",
    )


def _completed(
    arguments: tuple[str, ...], payload: object, *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        arguments, returncode, stdout=json.dumps(payload) if returncode == 0 else "", stderr=stderr
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


def _artifact() -> ReleaseArtifact:
    return ReleaseArtifact(
        version="0.1.0",
        digest=Digest.of_bytes(b"wheel-bytes"),
        media_type="application/vnd.pypa.wheel",
    )


def _publication_request(artifact: ReleaseArtifact) -> PublicationRequest:
    return PublicationRequest(
        run_id=RunId("run-1"),
        artifact=artifact,
        destination="github-release",
        operation_id=OperationId("publish-1"),
    )


def _release_payload(*, commitish: str = _COMMIT, body: str = "") -> dict[str, object]:
    return {
        "tag_name": "enginery-stage2-fixture-v0.1.0",
        "target_commitish": commitish,
        "name": "enginery-stage2-fixture v0.1.0",
        "body": body,
        "draft": False,
        "prerelease": False,
    }


def test_publish_creates_a_release_with_digest_evidence_in_the_body() -> None:
    calls: list[tuple[str, ...]] = []
    artifact = _artifact()
    responses: list[object] = [
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found"),  # find_by_tag
        _release_payload(),
    ]
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner(responses, calls))
    adapter.stage(
        artifact.digest,
        GitHubReleaseRequest(
            tag_name="enginery-stage2-fixture-v0.1.0",
            target_commitish=_COMMIT,
            name="enginery-stage2-fixture v0.1.0",
            body="Initial fixture release.",
        ),
    )

    receipt = adapter.publish(_publication_request(artifact))

    assert receipt.version == "0.1.0"
    assert receipt.artifact_digest == artifact.digest
    create_call = calls[1]
    assert "POST" in create_call
    body_field = next(field for field in create_call if field.startswith("body="))
    assert str(artifact.digest) in body_field


def test_publish_without_staging_raises() -> None:
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner([], []))

    with pytest.raises(InvalidInputError):
        adapter.publish(_publication_request(_artifact()))


def test_publish_is_idempotent_when_the_release_already_exists_at_the_expected_commit() -> None:
    calls: list[tuple[str, ...]] = []
    artifact = _artifact()
    responses: list[object] = [_release_payload(commitish=_COMMIT)]
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner(responses, calls))
    adapter.stage(
        artifact.digest,
        GitHubReleaseRequest(
            tag_name="enginery-stage2-fixture-v0.1.0",
            target_commitish=_COMMIT,
            name="enginery-stage2-fixture v0.1.0",
            body="Initial fixture release.",
        ),
    )

    receipt = adapter.publish(_publication_request(artifact))

    assert receipt.version == "0.1.0"
    assert len(calls) == 1  # only the existence check; no create attempted


def test_publish_raises_when_the_existing_release_points_at_a_different_commit() -> None:
    calls: list[tuple[str, ...]] = []
    artifact = _artifact()
    responses: list[object] = [_release_payload(commitish="d" * 40)]
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner(responses, calls))
    adapter.stage(
        artifact.digest,
        GitHubReleaseRequest(
            tag_name="enginery-stage2-fixture-v0.1.0",
            target_commitish=_COMMIT,
            name="enginery-stage2-fixture v0.1.0",
            body="Initial fixture release.",
        ),
    )

    with pytest.raises(ExternalConflictError, match="different commit"):
        adapter.publish(_publication_request(artifact))


def test_verify_confirms_commit_and_digest_evidence() -> None:
    calls: list[tuple[str, ...]] = []
    artifact = _artifact()
    marker = f"<!-- enginery:artifact-digest:{artifact.digest} -->"
    responses: list[object] = [
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found"),
        _release_payload(),
        _release_payload(body=f"Initial fixture release.\n\n{marker}"),
    ]
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner(responses, calls))
    request = GitHubReleaseRequest(
        tag_name="enginery-stage2-fixture-v0.1.0",
        target_commitish=_COMMIT,
        name="enginery-stage2-fixture v0.1.0",
        body="Initial fixture release.",
    )
    adapter.stage(artifact.digest, request)
    receipt = adapter.publish(_publication_request(artifact))

    verified = adapter.verify(receipt)

    assert verified == receipt


def test_verify_rejects_missing_digest_evidence() -> None:
    calls: list[tuple[str, ...]] = []
    artifact = _artifact()
    responses: list[object] = [
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found"),
        _release_payload(),
        _release_payload(body="No evidence marker here."),
    ]
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner(responses, calls))
    request = GitHubReleaseRequest(
        tag_name="enginery-stage2-fixture-v0.1.0",
        target_commitish=_COMMIT,
        name="enginery-stage2-fixture v0.1.0",
        body="Initial fixture release.",
    )
    adapter.stage(artifact.digest, request)
    receipt = adapter.publish(_publication_request(artifact))

    with pytest.raises(ExternalConflictError, match="digest evidence"):
        adapter.verify(receipt)


def test_reconcile_after_publish_reports_found_matching() -> None:
    calls: list[tuple[str, ...]] = []
    artifact = _artifact()
    responses: list[object] = [
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found"),
        _release_payload(),
        _release_payload(),  # reconcile() re-queries current state, not a cached outcome
    ]
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner(responses, calls))
    request = GitHubReleaseRequest(
        tag_name="enginery-stage2-fixture-v0.1.0",
        target_commitish=_COMMIT,
        name="enginery-stage2-fixture v0.1.0",
        body="Initial fixture release.",
    )
    adapter.stage(artifact.digest, request)
    publication_request = _publication_request(artifact)
    adapter.publish(publication_request)

    outcome = adapter.reconcile(operation_id=publication_request.operation_id)

    assert outcome is ReconciliationResult.FOUND_MATCHING


def test_reconcile_before_any_publish_attempt_reports_not_found() -> None:
    adapter = GitHubReleaseAdapter(_config(), command_runner=_runner([], []))

    outcome = adapter.reconcile(operation_id=OperationId("never-attempted"))

    assert outcome is ReconciliationResult.NOT_FOUND
