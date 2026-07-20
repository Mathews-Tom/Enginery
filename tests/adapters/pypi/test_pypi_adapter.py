from __future__ import annotations

import json
import subprocess
import urllib.error
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.delivery_ports import (
    PublicationReceipt,
    PublicationRequest,
    ReleaseArtifact,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.ids import OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult


def _config() -> PyPiAdapterConfig:
    return PyPiAdapterConfig(
        project_name="enginery-stage2-fixture",
        index_url="https://test.pypi.org/simple/",
        publish_url="https://test.pypi.org/legacy/",
        json_api_base="https://test.pypi.org/pypi",
    )


def _artifact(digest_bytes: bytes = b"wheel-bytes") -> ReleaseArtifact:
    return ReleaseArtifact(
        version="0.1.0",
        digest=Digest.of_bytes(digest_bytes),
        media_type="application/vnd.pypa.wheel",
    )


def _publication_request(artifact: ReleaseArtifact) -> PublicationRequest:
    return PublicationRequest(
        run_id=RunId("run-1"),
        artifact=artifact,
        destination="test-pypi",
        operation_id=OperationId("publish-1"),
    )


class _RecordingRunner:
    def __init__(self, result: subprocess.CompletedProcess[str]) -> None:
        self.result = result
        self.calls: list[tuple[Sequence[str], Path]] = []

    def __call__(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(command), cwd))
        return self.result


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), 0, stdout="", stderr="")


def test_config_rejects_non_https_urls() -> None:
    with pytest.raises(InvalidInputError):
        PyPiAdapterConfig(
            project_name="x",
            index_url="http://test.pypi.org/simple/",
            publish_url="https://test.pypi.org/legacy/",
            json_api_base="https://test.pypi.org/pypi",
        )


def test_publish_without_staging_raises() -> None:
    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(_ok()))

    with pytest.raises(InvalidInputError):
        adapter.publish(_publication_request(_artifact()))


def test_publish_invokes_uv_publish_with_the_staged_file_and_no_secret_arguments(
    tmp_path: Path,
) -> None:
    artifact = _artifact()
    wheel_path = tmp_path / "enginery_stage2_fixture-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"wheel-bytes")
    runner = _RecordingRunner(_ok())
    adapter = PyPiAdapter(_config(), command_runner=runner)
    adapter.stage(wheel_path)

    receipt = adapter.publish(_publication_request(artifact))

    assert receipt.version == "0.1.0"
    command, cwd = runner.calls[0]
    assert command[0] == "uv"
    assert "publish" in command
    assert "--publish-url" in command
    assert "https://test.pypi.org/legacy/" in command
    assert str(wheel_path) in command
    # No credential value ever appears as a literal command argument -- uv
    # reads UV_PUBLISH_TOKEN from the inherited environment on its own.
    assert not any("token" in argument.lower() for argument in command)
    assert not any(argument.startswith("pypi-") for argument in command)
    assert cwd == wheel_path.parent


def test_publish_raises_when_uv_publish_fails(tmp_path: Path) -> None:
    artifact = _artifact()
    wheel_path = tmp_path / "enginery_stage2_fixture-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"wheel-bytes")
    failing = subprocess.CompletedProcess((), 1, stdout="", stderr="403 Forbidden")
    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(failing))
    adapter.stage(wheel_path)

    with pytest.raises(ExternalConflictError, match="publish failed"):
        adapter.publish(_publication_request(artifact))


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _project_payload(*, sha256: str) -> dict[str, object]:
    return {"urls": [{"filename": "x.whl", "digests": {"sha256": sha256}}]}


def test_verify_confirms_the_reported_digest_matches() -> None:
    artifact = _artifact()

    receipt = PublicationReceipt(
        destination="test-pypi", version="0.1.0", artifact_digest=artifact.digest
    )

    def opener(url: str) -> bytes:
        assert url == "https://test.pypi.org/pypi/enginery-stage2-fixture/0.1.0/json"
        return _json_bytes(_project_payload(sha256=artifact.digest.hex_value))

    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(_ok()), url_opener=opener)

    verified = adapter.verify(receipt)

    assert verified == receipt


def test_verify_raises_when_the_reported_digest_does_not_match() -> None:

    artifact = _artifact()
    receipt = PublicationReceipt(
        destination="test-pypi", version="0.1.0", artifact_digest=artifact.digest
    )

    def opener(url: str) -> bytes:
        del url
        return _json_bytes(_project_payload(sha256="f" * 64))

    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(_ok()), url_opener=opener)

    with pytest.raises(ExternalConflictError, match="does not report a file"):
        adapter.verify(receipt)


def test_verify_raises_when_the_version_is_not_yet_reported() -> None:

    artifact = _artifact()
    receipt = PublicationReceipt(
        destination="test-pypi", version="0.1.0", artifact_digest=artifact.digest
    )

    def opener(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]

    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(_ok()), url_opener=opener)

    with pytest.raises(ExternalConflictError, match="does not yet report"):
        adapter.verify(receipt)


def test_reconcile_before_any_publish_attempt_reports_not_found() -> None:
    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(_ok()))

    outcome = adapter.reconcile(operation_id=OperationId("never-attempted"))

    assert outcome is ReconciliationResult.NOT_FOUND


def test_reconcile_after_a_successful_publish_reports_found_matching(tmp_path: Path) -> None:
    artifact = _artifact()
    wheel_path = tmp_path / "enginery_stage2_fixture-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"wheel-bytes")
    adapter = PyPiAdapter(_config(), command_runner=_RecordingRunner(_ok()))
    adapter.stage(wheel_path)
    request = _publication_request(artifact)
    adapter.publish(request)

    outcome = adapter.reconcile(operation_id=request.operation_id)

    assert outcome is ReconciliationResult.FOUND_MATCHING
