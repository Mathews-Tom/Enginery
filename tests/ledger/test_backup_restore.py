from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.ledger.artifacts import ArtifactMetadataWrite
from enginery.ledger.backup import backup_ledger, read_backup_manifest, restore_ledger
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.ledger.verify import verify_ledger


def _seed(database: Path, artifacts: Path) -> str:
    service = LedgerService.open(database, artifact_store_root=artifacts)
    try:
        digest = service.publish_artifact_bytes(b"artifact bytes", media_type="text/plain")
        service.append(
            AppendCommand(
                correlation_id="cmd-1",
                events=(
                    EventWrite(
                        aggregate_type="work_item",
                        aggregate_id="wi-1",
                        expected_version=0,
                        event_type="work_item.created",
                        schema_version=1,
                        payload={"title": "demo"},
                    ),
                ),
                artifact_references=(
                    ArtifactMetadataWrite(
                        artifact_id=ArtifactId("art-1"),
                        digest=digest,
                        byte_size=len(b"artifact bytes"),
                        media_type="text/plain",
                        kind=ArtifactKind.LOG,
                        run_id=RunId("run-1"),
                        node_id=NodeId("normalize"),
                        attempt_id=NodeAttemptId("attempt-1"),
                        redaction=RedactionClassification.INTERNAL,
                    ),
                ),
            )
        )
        return str(digest)
    finally:
        service.close()


def test_backup_reproduces_aggregate_and_artifact_state_on_restore(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    digest = _seed(database, artifacts)

    backup_dir = tmp_path / "backup"
    manifest = backup_ledger(database, backup_dir, artifact_store_root=artifacts)
    assert manifest.includes_artifacts is True

    restored_database = tmp_path / "restored" / "ledger.db"
    restored_artifacts = tmp_path / "restored" / "artifacts"
    restore_ledger(
        backup_dir, restored_database, destination_artifact_store_root=restored_artifacts
    )

    service = LedgerService.open(restored_database, artifact_store_root=restored_artifacts)
    try:
        projection = service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
        assert projection is not None
        assert projection.state == {"title": "demo"}

        metadata = service.read_artifact_metadata("art-1")
        assert metadata is not None
        assert metadata.digest == digest

        assert service.artifact_store is not None
        assert (
            service.artifact_store.read_bytes(
                service.artifact_store.publish_bytes(b"artifact bytes", media_type="text/plain")
            )
            == b"artifact bytes"
        )
    finally:
        service.close()

    report = verify_ledger(restored_database, artifact_store_root=restored_artifacts)
    assert report.healthy is True


def test_backup_without_artifact_store_omits_artifacts_directory(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    backup_dir = tmp_path / "backup"
    manifest = backup_ledger(database, backup_dir)

    assert manifest.includes_artifacts is False
    assert not (backup_dir / "artifacts").exists()


def test_backup_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    backup_dir = tmp_path / "backup"
    backup_ledger(database, backup_dir)

    with pytest.raises(InvalidInputError):
        backup_ledger(database, backup_dir)


def test_restore_refuses_to_overwrite_an_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()
    backup_dir = tmp_path / "backup"
    backup_ledger(database, backup_dir)

    already_exists = tmp_path / "existing.db"
    LedgerService.open(already_exists).close()

    with pytest.raises(InvalidInputError):
        restore_ledger(backup_dir, already_exists)


def test_read_backup_manifest_round_trips(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()
    backup_dir = tmp_path / "backup"
    written = backup_ledger(database, backup_dir)

    read_back = read_backup_manifest(backup_dir)

    assert read_back == written


def test_restore_without_destination_store_but_manifest_expects_one_raises(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    _seed(database, artifacts)
    backup_dir = tmp_path / "backup"
    backup_ledger(database, backup_dir, artifact_store_root=artifacts)

    with pytest.raises(InvalidInputError):
        restore_ledger(backup_dir, tmp_path / "restored" / "ledger.db")
