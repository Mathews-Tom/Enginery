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
        ["operator", "verify", "--database", str(database), "--artifacts", str(artifacts)]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "healthy"


def test_ledger_verify_json_output_reports_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    exit_code = main(["operator", "verify", "--database", str(database), "--json"])

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
        ["operator", "verify", "--database", str(database), "--artifacts", str(artifacts)]
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "artifact_bytes_missing_or_corrupted" in captured.err
    assert "unhealthy" in captured.out


def test_ledger_verify_on_unmigrated_database_reports_schema_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "fresh.db"

    exit_code = main(["operator", "verify", "--database", str(database)])

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
            "operator",
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
    backup_report = json.loads(capsys.readouterr().out)
    assert backup_report["backup"] == str(backup_dir)
    assert backup_report["includes_artifacts"] is True
    assert backup_report["verified_before"]["healthy"] is True
    assert backup_report["verified_after"]["healthy"] is True

    restored_database = tmp_path / "restored" / "ledger.db"
    restored_artifacts = tmp_path / "restored" / "artifacts"
    restored_database.parent.mkdir()
    LedgerService.open(restored_database).close()
    assert main(["operator", "pause", "--database", str(restored_database)]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] is True
    restore_exit = main(
        [
            "operator",
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
    restore_report = json.loads(capsys.readouterr().out)
    assert restore_report["restored"] == str(restored_database)
    assert restore_report["backup"] == str(backup_dir)
    assert restore_report["prior"] == "paused"
    assert restore_report["paused"] is True
    assert Path(restore_report["prior_ledger_kept_at"]).is_file()
    assert restore_report["verified_backup"]["healthy"] is True
    assert restore_report["verified_restored"]["healthy"] is True

    verify_exit = main(
        [
            "operator",
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

    exit_code = main(["operator", "rebuild-projections", "--database", str(database)])

    assert exit_code == 0
    assert "rebuilt 1 projection" in capsys.readouterr().out


def test_ledger_without_subcommand_errors(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["operator"])

    assert exit_code == 2
    assert "enginery operator" in capsys.readouterr().err


def test_ledger_verify_against_the_checked_in_fixture(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A historical ledger remains healthy after the normal schema upgrade."""
    from enginery.ledger.service import LedgerService

    historical = Path("tests/fixtures/ledger.db")
    assert historical.is_file()
    fixture = tmp_path / "ledger.db"
    fixture.write_bytes(historical.read_bytes())
    with LedgerService.open(fixture):
        pass

    exit_code = main(["operator", "verify", "--database", str(fixture)])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "healthy"
