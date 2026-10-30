from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.cli._exit_codes import exit_code_for
from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.engine.runtime import CoordinatorRuntime, WorkflowNodeDispatch
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.artifacts import ArtifactMetadataWrite, apply_artifact_metadata
from enginery.ledger.service import LedgerService
from tests.cli.test_engine_runs import _dispatch, _lifecycle, _start_request

VALIDATION_ID = "node:qualify:validation_artifact_digest"
INTEGRITY_EXIT = exit_code_for(FailureClass.VALIDATION_FAILURE)


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _setup(
    database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    node_extra: dict[str, object] | None = None,
) -> ArtifactStore:
    """A started run with two artifacts-table rows and a runtime node digest.

    ``node_extra`` overrides fields of the completed runtime node's recorded state.
    """
    request = _start_request(database, tmp_path)
    store = ArtifactStore(request.execution_configuration.artifact_root)
    with LedgerService.open(database, artifact_store_root=store.root) as ledger:
        with ledger.connection:
            for artifact_id, data in (("plan", b"durable plan\n"), ("log", b"durable log\n")):
                apply_artifact_metadata(
                    ledger.connection,
                    ArtifactMetadataWrite(
                        artifact_id=ArtifactId(artifact_id),
                        digest=store.publish_bytes(data, media_type="text/plain"),
                        byte_size=len(data),
                        media_type="text/plain",
                        kind=ArtifactKind.LOG,
                        run_id=RunId("run-stage1"),
                        node_id=NodeId("qualify"),
                        attempt_id=NodeAttemptId("qualify-0"),
                        redaction=RedactionClassification.PUBLIC,
                    ),
                    store=store,
                )
        runtime = CoordinatorRuntime(ledger, owner="stage1-cli")
        now = datetime.now(tz=UTC)
        epoch = runtime.register_node(
            dispatch=WorkflowNodeDispatch(
                _dispatch(request, node_id="qualify", dependencies=()), request.manifest
            ),
            now=now,
            heartbeat_window=timedelta(seconds=60),
        )
        runtime.complete_node(
            run_id="run-stage1",
            node_id="qualify",
            epoch=epoch.epoch,
            now=now,
            extra={
                "validation_artifact_digest": str(
                    store.publish_bytes(b"validation report\n", media_type="text/plain")
                ),
                **(node_extra or {}),
            },
        )
    capsys.readouterr()
    return store


def _verify(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object], dict[str, str]]:
    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")
    document = json.loads(capsys.readouterr().out)
    verification = document["verification"]
    statuses = {entry["artifact_id"]: entry["status"] for entry in verification["artifacts"]}
    return code, verification, statuses


def _path(store: ArtifactStore, data: bytes) -> Path:
    return store.path_for(Digest.of_bytes(data))


def test_verify_reports_every_referenced_artifact_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    store = _setup(database, tmp_path, capsys)

    code, verification, statuses = _verify(database, capsys)

    assert code == 0
    assert statuses == {"plan": "intact", "log": "intact", VALIDATION_ID: "intact"}
    assert verification["artifact_root"] == str(store.root)
    assert verification["summary"] == {"intact": 3, "missing": 0, "tampered": 0}


def test_verify_reports_a_deleted_artifact_missing_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    store = _setup(database, tmp_path, capsys)
    _path(store, b"durable plan\n").unlink()
    ledger_before, store_before = database.read_bytes(), _tree(store.root)

    code, verification, statuses = _verify(database, capsys)

    assert code == INTEGRITY_EXIT
    assert statuses == {"plan": "missing", "log": "intact", VALIDATION_ID: "intact"}
    missing = next(entry for entry in verification["artifacts"] if entry["artifact_id"] == "plan")
    assert missing["digest"] == str(Digest.of_bytes(b"durable plan\n"))
    assert verification["summary"] == {"intact": 2, "missing": 1, "tampered": 0}
    assert database.read_bytes() == ledger_before
    assert _tree(store.root) == store_before


def test_verify_reports_overwritten_bytes_tampered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    store = _setup(database, tmp_path, capsys)
    _path(store, b"durable log\n").write_bytes(b"durable lOg\n")
    store_before = _tree(store.root)

    code, verification, statuses = _verify(database, capsys)

    assert code == INTEGRITY_EXIT
    assert statuses == {"plan": "intact", "log": "tampered", VALIDATION_ID: "intact"}
    assert verification["summary"] == {"intact": 2, "missing": 0, "tampered": 1}
    assert _tree(store.root) == store_before


def test_verify_covers_a_runtime_node_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    store = _setup(database, tmp_path, capsys)
    _path(store, b"validation report\n").unlink()

    code, _, statuses = _verify(database, capsys)

    assert code == INTEGRITY_EXIT
    assert statuses[VALIDATION_ID] == "missing"


def test_evidence_without_verify_is_unchanged_by_artifact_damage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    store = _setup(database, tmp_path, capsys)
    assert _lifecycle(database, "evidence", "--run-id", "run-stage1") == 0
    before = capsys.readouterr().out
    _path(store, b"durable plan\n").unlink()

    assert _lifecycle(database, "evidence", "--run-id", "run-stage1") == 0
    after = capsys.readouterr().out

    assert after == before
    assert "verification" not in json.loads(after)


def test_verify_with_a_missing_database_reports_it_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "absent.db"

    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "existing engine ledger" in capsys.readouterr().err
    assert not database.exists()


def test_verify_refuses_an_unresolvable_artifact_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    store = _setup(database, tmp_path, capsys)
    for path in sorted(store.root.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    store.root.rmdir()

    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")

    captured = capsys.readouterr()
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "artifact store root" in captured.err
    assert captured.out == ""
    assert not store.root.exists()


@pytest.mark.parametrize("recorded", ["", 0, False, None, {"0": "sha256:0"}])
def test_verify_rejects_present_artifact_references_that_are_not_a_list(
    recorded: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    _setup(database, tmp_path, capsys, node_extra={"artifact_references": recorded})

    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")

    captured = capsys.readouterr()
    assert code == INTEGRITY_EXIT
    assert "recorded artifact references are malformed" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("recorded", ["sha256", "md5:abc", "sha256:not-hex", 7])
def test_verify_rejects_a_malformed_recorded_digest(
    recorded: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    _setup(database, tmp_path, capsys, node_extra={"validation_artifact_digest": recorded})

    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")

    captured = capsys.readouterr()
    assert code == INTEGRITY_EXIT
    assert "malformed artifact digest" in captured.err
    assert captured.out == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
def test_verify_names_a_ledger_whose_directory_is_not_writable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_directory = tmp_path / "ledger"
    ledger_directory.mkdir()
    database = ledger_directory / "ledger.db"
    _setup(database, tmp_path, capsys)
    ledger_directory.chmod(0o555)
    try:
        code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")
        listing = sorted(path.name for path in ledger_directory.iterdir())
    finally:
        ledger_directory.chmod(0o755)

    captured = capsys.readouterr()
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert f"cannot read the ledger read-only at {database}" in captured.err
    assert "make the ledger's directory writable" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert listing == ["ledger.db"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_verify_names_a_ledger_file_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    _setup(database, tmp_path, capsys)
    database.chmod(0o000)
    try:
        code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")
    finally:
        database.chmod(0o644)

    captured = capsys.readouterr()
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert f"cannot read the ledger read-only at {database}" in captured.err
    assert captured.out == ""


def test_verify_names_a_ledger_that_is_not_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    database.write_bytes(b"not a sqlite database\n" * 64)

    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")

    captured = capsys.readouterr()
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert f"cannot read the ledger read-only at {database}" in captured.err
    assert "file is not a database" in captured.err
    assert captured.out == ""
    assert database.read_bytes() == b"not a sqlite database\n" * 64


def test_verify_names_an_empty_file_as_no_engine_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    database.touch()

    code = _lifecycle(database, "evidence", "--run-id", "run-stage1", "--verify")

    captured = capsys.readouterr()
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert f"{database} has no ledger tables" in captured.err
    assert captured.out == ""
    assert database.read_bytes() == b""
