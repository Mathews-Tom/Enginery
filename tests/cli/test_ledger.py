from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginery.cli.main import main
from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.ledger.artifacts import ArtifactMetadataWrite
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService


def _seed_ledger_with_artifact(database: Path, artifacts: Path) -> None:
    service = LedgerService.open(database, artifact_store_root=artifacts)
    try:
        digest = service.publish_artifact_bytes(b"log output", media_type="text/plain")
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
                        payload={"title": "demo"},
                    ),
                ),
                artifact_references=(
                    ArtifactMetadataWrite(
                        artifact_id=ArtifactId("art-1"),
                        digest=digest,
                        byte_size=len(b"log output"),
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
    finally:
        service.close()


def test_ledger_verify_reports_healthy_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    _seed_ledger_with_artifact(database, artifacts)

    exit_code = main(
        ["ledger", "verify", "--database", str(database), "--artifacts", str(artifacts)]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "healthy"


def test_ledger_verify_json_output_reports_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    exit_code = main(["ledger", "verify", "--database", str(database), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["issues"] == []
    assert payload["schema_version"] >= 1


def test_ledger_verify_detects_tampered_artifact_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    _seed_ledger_with_artifact(database, artifacts)
    for path in (artifacts / "objects").rglob("*"):
        if path.is_file():
            path.write_bytes(b"tampered bytes")

    exit_code = main(
        ["ledger", "verify", "--database", str(database), "--artifacts", str(artifacts)]
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "artifact_bytes_missing_or_corrupted" in captured.err
    assert "unhealthy" in captured.out


def test_ledger_verify_on_unmigrated_database_reports_schema_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "fresh.db"

    exit_code = main(["ledger", "verify", "--database", str(database)])

    assert exit_code != 0
    assert "unhealthy" in capsys.readouterr().out


def test_ledger_backup_and_restore_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    _seed_ledger_with_artifact(database, artifacts)

    backup_dir = tmp_path / "backup"
    backup_exit = main(
        [
            "ledger",
            "backup",
            "--database",
            str(database),
            "--output",
            str(backup_dir),
            "--artifacts",
            str(artifacts),
        ]
    )
    assert backup_exit == 0
    assert "backup written to" in capsys.readouterr().out

    restored_database = tmp_path / "restored" / "ledger.db"
    restored_artifacts = tmp_path / "restored" / "artifacts"
    restore_exit = main(
        [
            "ledger",
            "restore",
            "--backup",
            str(backup_dir),
            "--database",
            str(restored_database),
            "--artifacts",
            str(restored_artifacts),
        ]
    )
    assert restore_exit == 0
    assert "restored" in capsys.readouterr().out

    verify_exit = main(
        [
            "ledger",
            "verify",
            "--database",
            str(restored_database),
            "--artifacts",
            str(restored_artifacts),
        ]
    )
    assert verify_exit == 0
    assert capsys.readouterr().out.strip() == "healthy"


def test_ledger_rebuild_projections_reports_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    artifacts = tmp_path / "artifacts"
    _seed_ledger_with_artifact(database, artifacts)

    exit_code = main(["ledger", "rebuild-projections", "--database", str(database)])

    assert exit_code == 0
    assert "rebuilt 1 projection" in capsys.readouterr().out


def test_ledger_without_subcommand_errors(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["ledger"])

    assert exit_code != 0
    assert "ledger subcommand is required" in capsys.readouterr().err


def test_ledger_verify_against_the_checked_in_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Matches the milestone verification command exactly:
    ``enginery ledger verify --database tests/fixtures/ledger.db``."""
    fixture = Path("tests/fixtures/ledger.db")
    assert fixture.is_file()

    exit_code = main(["ledger", "verify", "--database", str(fixture)])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "healthy"
