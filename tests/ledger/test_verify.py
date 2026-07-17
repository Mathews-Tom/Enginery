from __future__ import annotations

from pathlib import Path

from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.ledger.artifacts import ArtifactMetadataWrite
from enginery.ledger.connection import open_connection
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.migrations import apply_pending_migrations
from enginery.ledger.schema import MIGRATIONS
from enginery.ledger.service import LedgerService
from enginery.ledger.verify import verify_ledger


def test_verify_reports_healthy_for_a_freshly_migrated_ledger(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    report = verify_ledger(database)

    assert report.healthy is True
    assert report.issues == ()
    assert report.schema_version == MIGRATIONS[-1].version


def test_verify_reports_stale_schema_when_a_migration_is_pending(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    connection = open_connection(database)
    apply_pending_migrations(connection, migrations=MIGRATIONS[:1])
    connection.close()

    report = verify_ledger(database)

    assert report.healthy is False
    assert any(issue.code == "schema_version_stale" for issue in report.issues)
    assert report.schema_version == MIGRATIONS[0].version


def test_verify_reports_corrupted_sqlite_file(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()
    with database.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\xff" * 64)

    report = verify_ledger(database)

    assert report.healthy is False
    assert any(
        issue.code in {"integrity_check_failed", "database_unreadable"} for issue in report.issues
    )


def test_verify_with_no_artifact_store_skips_artifact_checks(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    report = verify_ledger(database, artifact_store_root=None)

    assert report.healthy is True


def test_verify_detects_missing_artifact_bytes(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    service = LedgerService.open(database, artifact_store_root=artifacts)
    digest = service.publish_artifact_bytes(b"payload", media_type="text/plain")
    service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(
                EventWrite(
                    aggregate_type="run",
                    aggregate_id="run-1",
                    expected_version=0,
                    event_type="run.created",
                    schema_version=1,
                    payload={},
                ),
            ),
            artifact_references=(
                ArtifactMetadataWrite(
                    artifact_id=ArtifactId("art-1"),
                    digest=digest,
                    byte_size=len(b"payload"),
                    media_type="text/plain",
                    kind=ArtifactKind.LOG,
                    run_id=RunId("run-1"),
                    node_id=NodeId("n"),
                    attempt_id=NodeAttemptId("a1"),
                    redaction=RedactionClassification.INTERNAL,
                ),
            ),
        )
    )
    assert service.artifact_store is not None
    service.artifact_store.path_for(digest).unlink()
    service.close()

    report = verify_ledger(database, artifact_store_root=artifacts)

    assert report.healthy is False
    assert any(issue.code == "artifact_bytes_missing_or_corrupted" for issue in report.issues)
