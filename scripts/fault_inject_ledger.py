#!/usr/bin/env python3
"""Ledger fault-injection gate.

A thin entry point over ``scripts/fault_injection/framework.py``: this
script only defines the ledger-specific scenarios and hands them to the
shared runner. Exit code ``0`` iff every scenario below passes.

Scenarios: expected-version conflict, multi-aggregate commit rollback,
interrupted write (real process kill mid-transaction), corrupted event
payload, missing artifact bytes, artifact digest mismatch, failed
migration, backup during idle state, restore, and projection rebuild.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.ledger.artifacts import ArtifactMetadataWrite
from enginery.ledger.backup import backup_ledger, restore_ledger
from enginery.ledger.connection import open_connection
from enginery.ledger.errors import (
    ArtifactDigestMismatchError,
    ArtifactMissingError,
    CorruptedEventError,
    ExpectedVersionConflictError,
    MigrationFailedError,
)
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.migrations import apply_pending_migrations
from enginery.ledger.projections import rebuild_projections
from enginery.ledger.schema import MIGRATIONS, Migration
from enginery.ledger.service import LedgerService
from enginery.ledger.verify import verify_ledger
from fault_injection.framework import FaultScenario, main_for


def _event(**overrides: object) -> EventWrite:
    defaults: dict[str, object] = {
        "aggregate_type": "work_item",
        "aggregate_id": "wi-1",
        "expected_version": 0,
        "event_type": "work_item.created",
        "schema_version": 1,
        "payload": {"title": "fault-injection fixture"},
    }
    defaults.update(overrides)
    return EventWrite(**defaults)  # type: ignore[arg-type]


def _scenario_expected_version_conflict() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = LedgerService.open(Path(tmp) / "ledger.db")
        try:
            service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
            try:
                service.append(
                    AppendCommand(
                        correlation_id="cmd-2",
                        events=(_event(expected_version=0, event_type="work_item.qualified"),),
                    )
                )
            except ExpectedVersionConflictError:
                pass
            else:
                raise AssertionError("stale expected_version did not raise")

            row = service.connection.execute(
                "SELECT version FROM aggregates WHERE aggregate_type = 'work_item' "
                "AND aggregate_id = 'wi-1'"
            ).fetchone()
            if row["version"] != 1:
                raise AssertionError(f"aggregate version drifted to {row['version']}")
        finally:
            service.close()


def _scenario_commit_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = LedgerService.open(Path(tmp) / "ledger.db")
        try:
            service.append(
                AppendCommand(
                    correlation_id="setup",
                    events=(_event(aggregate_type="run", aggregate_id="run-1"),),
                )
            )
            try:
                service.append(
                    AppendCommand(
                        correlation_id="cmd-multi",
                        events=(
                            _event(aggregate_type="work_item", aggregate_id="wi-multi"),
                            _event(
                                aggregate_type="run",
                                aggregate_id="run-1",
                                expected_version=0,
                                event_type="run.queued",
                            ),
                        ),
                    )
                )
            except ExpectedVersionConflictError:
                pass
            else:
                raise AssertionError("multi-aggregate conflict did not raise")

            leaked = service.connection.execute(
                "SELECT COUNT(*) AS n FROM aggregates WHERE aggregate_type = 'work_item' "
                "AND aggregate_id = 'wi-multi'"
            ).fetchone()["n"]
            if leaked != 0:
                raise AssertionError("first event of a failed multi-aggregate command leaked")
        finally:
            service.close()


def _scenario_interrupted_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "ledger.db"
        LedgerService.open(db_path).close()

        marker = Path(tmp) / "written.marker"
        script_path = Path(tmp) / "crash_writer.py"
        script_path.write_text(
            "import sqlite3\n"
            f"conn = sqlite3.connect({str(db_path)!r}, isolation_level=None)\n"
            'conn.execute("BEGIN IMMEDIATE")\n'
            "conn.execute(\n"
            '    "INSERT INTO events (aggregate_type, aggregate_id, aggregate_version, "\n'
            '    "event_type, schema_version, payload, correlation_id, causation_id, "\n'
            "    \"recorded_at) VALUES ('work_item', 'wi-crash', 1, 'work_item.created', \"\n"
            "    \"1, '{}', 'cmd-crash', 'cmd-crash', '2026-01-01T00:00:00+00:00')\"\n"
            ")\n"
            f"open({str(marker)!r}, 'w').close()\n"
            "import time\n"
            "time.sleep(30)\n"
        )

        process = subprocess.Popen([sys.executable, str(script_path)])
        deadline = time.time() + 10
        try:
            while not marker.exists():
                if time.time() > deadline:
                    raise AssertionError("writer process never reached the pre-kill marker")
                time.sleep(0.02)
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        service = LedgerService.open(db_path)
        try:
            leaked = service.connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE aggregate_id = 'wi-crash'"
            ).fetchone()["n"]
            if leaked != 0:
                raise AssertionError(f"interrupted write leaked {leaked} uncommitted event row(s)")
            service.append(AppendCommand(correlation_id="post-crash", events=(_event(),)))
        finally:
            service.close()


def _scenario_corrupted_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = LedgerService.open(Path(tmp) / "ledger.db")
        try:
            service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
            service.connection.execute(
                "UPDATE events SET payload = '{not valid json' "
                "WHERE aggregate_type = 'work_item' AND aggregate_id = 'wi-1'"
            )
            try:
                rebuild_projections(service.connection)
            except CorruptedEventError:
                pass
            else:
                raise AssertionError("corrupted event payload did not raise on rebuild")
        finally:
            service.close()


def _scenario_missing_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = LedgerService.open(
            Path(tmp) / "ledger.db", artifact_store_root=Path(tmp) / "artifacts"
        )
        try:
            fabricated_digest = Digest.of_bytes(b"never published")
            try:
                service.append(
                    AppendCommand(
                        correlation_id="cmd-1",
                        events=(_event(aggregate_type="run", aggregate_id="run-1"),),
                        artifact_references=(
                            ArtifactMetadataWrite(
                                artifact_id=ArtifactId("art-missing"),
                                digest=fabricated_digest,
                                byte_size=1,
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
            except ArtifactMissingError:
                pass
            else:
                raise AssertionError("unpublished artifact digest did not raise")
        finally:
            service.close()


def _scenario_digest_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        artifact_root = Path(tmp) / "artifacts"
        service = LedgerService.open(Path(tmp) / "ledger.db", artifact_store_root=artifact_root)
        try:
            digest = service.publish_artifact_bytes(b"trustworthy bytes", media_type="text/plain")
            service.append(
                AppendCommand(
                    correlation_id="cmd-1",
                    events=(_event(aggregate_type="run", aggregate_id="run-1"),),
                    artifact_references=(
                        ArtifactMetadataWrite(
                            artifact_id=ArtifactId("art-1"),
                            digest=digest,
                            byte_size=len(b"trustworthy bytes"),
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
            service.artifact_store.path_for(digest).write_bytes(b"tampered")
            try:
                service.artifact_store.read_bytes(digest)
            except ArtifactDigestMismatchError:
                pass
            else:
                raise AssertionError("tampered artifact bytes did not raise on read")

            report = verify_ledger(Path(tmp) / "ledger.db", artifact_store_root=artifact_root)
            if report.healthy:
                raise AssertionError("verify reported healthy despite a digest mismatch")
        finally:
            service.close()


def _scenario_failed_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "ledger.db"
        connection = open_connection(db_path)
        try:
            apply_pending_migrations(connection, migrations=MIGRATIONS)
            version_before = MIGRATIONS[-1].version
            broken = Migration(
                version=version_before + 1,
                description="deliberately broken",
                statements=("CREATE TABLE this is not valid sql (",),
            )
            try:
                apply_pending_migrations(connection, migrations=(*MIGRATIONS, broken))
            except MigrationFailedError:
                pass
            else:
                raise AssertionError("broken migration did not raise")

            report = verify_ledger(db_path)
            if report.schema_version != version_before:
                raise AssertionError(
                    f"schema version drifted to {report.schema_version} after a failed migration"
                )
        finally:
            connection.close()


def _scenario_backup_during_idle_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "ledger.db"
        artifact_root = Path(tmp) / "artifacts"
        service = LedgerService.open(db_path, artifact_store_root=artifact_root)
        digest = service.publish_artifact_bytes(b"idle backup fixture", media_type="text/plain")
        service.append(
            AppendCommand(
                correlation_id="cmd-1",
                events=(_event(aggregate_type="run", aggregate_id="run-1"),),
                artifact_references=(
                    ArtifactMetadataWrite(
                        artifact_id=ArtifactId("art-1"),
                        digest=digest,
                        byte_size=len(b"idle backup fixture"),
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
        service.close()  # no coordinator process exists in this milestone: idle by construction

        backup_dir = Path(tmp) / "backup"
        manifest = backup_ledger(db_path, backup_dir, artifact_store_root=artifact_root)
        if not manifest.includes_artifacts:
            raise AssertionError("backup manifest did not record artifacts")
        if not (backup_dir / "ledger.db").is_file():
            raise AssertionError("backup did not write a database file")


def _scenario_restore() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "ledger.db"
        artifact_root = Path(tmp) / "artifacts"
        service = LedgerService.open(db_path, artifact_store_root=artifact_root)
        digest = service.publish_artifact_bytes(b"restore fixture", media_type="text/plain")
        service.append(
            AppendCommand(
                correlation_id="cmd-1",
                events=(_event(),),
                artifact_references=(
                    ArtifactMetadataWrite(
                        artifact_id=ArtifactId("art-1"),
                        digest=digest,
                        byte_size=len(b"restore fixture"),
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
        service.close()

        backup_dir = Path(tmp) / "backup"
        backup_ledger(db_path, backup_dir, artifact_store_root=artifact_root)

        restored_db = Path(tmp) / "restored" / "ledger.db"
        restored_artifacts = Path(tmp) / "restored" / "artifacts"
        restore_ledger(backup_dir, restored_db, destination_artifact_store_root=restored_artifacts)

        report = verify_ledger(restored_db, artifact_store_root=restored_artifacts)
        if not report.healthy:
            raise AssertionError(f"restored ledger is unhealthy: {report.issues}")

        restored_service = LedgerService.open(restored_db, artifact_store_root=restored_artifacts)
        try:
            projection = restored_service.read_projection(
                aggregate_type="work_item", aggregate_id="wi-1"
            )
            if projection is None or projection.state != {"title": "fault-injection fixture"}:
                raise AssertionError("restored projection state does not match the original")
            metadata = restored_service.read_artifact_metadata("art-1")
            if metadata is None or metadata.digest != str(digest):
                raise AssertionError("restored artifact metadata does not match the original")
        finally:
            restored_service.close()


def _scenario_projection_rebuild() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service = LedgerService.open(Path(tmp) / "ledger.db")
        try:
            service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
            service.append(
                AppendCommand(
                    correlation_id="cmd-2",
                    events=(
                        _event(
                            expected_version=1,
                            event_type="work_item.qualified",
                            payload={"title": "fault-injection fixture", "qualified": True},
                        ),
                    ),
                )
            )
            before = service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
            report = service.rebuild_projections()
            after = service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
            if report.aggregates_rebuilt != 1:
                raise AssertionError(
                    f"expected 1 rebuilt aggregate, got {report.aggregates_rebuilt}"
                )
            if before is None or after is None or before.state != after.state:
                raise AssertionError("rebuild did not reproduce the prior projection state")
        finally:
            service.close()


SCENARIOS = (
    FaultScenario(
        name="expected_version_conflict",
        description="a stale expected_version rejects atomically and writes nothing",
        run=_scenario_expected_version_conflict,
    ),
    FaultScenario(
        name="commit_rollback",
        description="a multi-aggregate command's failure undoes every event in the same command",
        run=_scenario_commit_rollback,
    ),
    FaultScenario(
        name="interrupted_write",
        description="a process killed mid-transaction leaves no partial event behind",
        run=_scenario_interrupted_write,
    ),
    FaultScenario(
        name="corrupted_event",
        description="a corrupted event payload fails projection rebuild loudly",
        run=_scenario_corrupted_event,
    ),
    FaultScenario(
        name="missing_artifact",
        description="metadata cannot reference a digest with no published bytes",
        run=_scenario_missing_artifact,
    ),
    FaultScenario(
        name="digest_mismatch",
        description="tampered artifact bytes fail both direct read and ledger verify",
        run=_scenario_digest_mismatch,
    ),
    FaultScenario(
        name="failed_migration",
        description="a broken migration leaves the prior schema version intact",
        run=_scenario_failed_migration,
    ),
    FaultScenario(
        name="backup_during_idle_state",
        description="a backup taken with no coordinator running captures a full manifest",
        run=_scenario_backup_during_idle_state,
    ),
    FaultScenario(
        name="restore",
        description="a restored ledger reproduces the same projection and artifact metadata",
        run=_scenario_restore,
    ),
    FaultScenario(
        name="projection_rebuild",
        description="rebuilding projections from events reproduces the prior projection state",
        run=_scenario_projection_rebuild,
    ),
)


if __name__ == "__main__":
    raise SystemExit(main_for(SCENARIOS))
