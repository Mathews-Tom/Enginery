"""Backup and restore.

Uses SQLite's native online backup API (``sqlite3.Connection.backup``),
which always produces a transactionally consistent snapshot regardless of
concurrent readers — the supported precondition the design states is "the
coordinator is stopped," proven at the fault-injection layer by taking a
backup with no coordinator process running at all. The artifact store
directory is copied alongside the database file so a restore reproduces
both the aggregate state and the artifact bytes it references.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from enginery.domain.errors import InvalidInputError
from enginery.ledger.migrations import current_schema_version

_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "ledger.db"
_ARTIFACTS_DIRNAME = "artifacts"


@dataclass(frozen=True, slots=True)
class BackupManifest:
    created_at: str
    schema_version: int
    source_database: str
    includes_artifacts: bool


def backup_ledger(
    database_path: Path,
    destination_dir: Path,
    *,
    artifact_store_root: Path | None = None,
) -> BackupManifest:
    """Snapshot ``database_path`` (and, if given, ``artifact_store_root``)
    into ``destination_dir``.

    Raises :class:`InvalidInputError` if ``destination_dir`` already
    contains a backup — a backup command never silently overwrites a
    prior one.
    """
    if destination_dir.exists() and any(destination_dir.iterdir()):
        raise InvalidInputError(
            f"destination directory {destination_dir} is not empty",
            details={"destination_dir": str(destination_dir)},
        )
    destination_dir.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(str(database_path))
    destination = sqlite3.connect(str(destination_dir / _DATABASE_NAME))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    schema_version_connection = sqlite3.connect(str(destination_dir / _DATABASE_NAME))
    schema_version_connection.row_factory = sqlite3.Row
    try:
        schema_version = current_schema_version(schema_version_connection)
    finally:
        schema_version_connection.close()

    includes_artifacts = artifact_store_root is not None
    if artifact_store_root is not None and artifact_store_root.is_dir():
        shutil.copytree(artifact_store_root, destination_dir / _ARTIFACTS_DIRNAME)

    manifest = BackupManifest(
        created_at=datetime.now(UTC).isoformat(),
        schema_version=schema_version,
        source_database=str(database_path),
        includes_artifacts=includes_artifacts,
    )
    (destination_dir / _MANIFEST_NAME).write_text(
        json.dumps(
            {
                "created_at": manifest.created_at,
                "schema_version": manifest.schema_version,
                "source_database": manifest.source_database,
                "includes_artifacts": manifest.includes_artifacts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def read_backup_manifest(backup_dir: Path) -> BackupManifest:
    raw = json.loads((backup_dir / _MANIFEST_NAME).read_text(encoding="utf-8"))
    return BackupManifest(
        created_at=raw["created_at"],
        schema_version=raw["schema_version"],
        source_database=raw["source_database"],
        includes_artifacts=raw["includes_artifacts"],
    )


def restore_ledger(
    backup_dir: Path,
    destination_database_path: Path,
    *,
    destination_artifact_store_root: Path | None = None,
) -> BackupManifest:
    """Restore a backup produced by :func:`backup_ledger`.

    Raises :class:`InvalidInputError` if ``destination_database_path``
    already exists — restore never silently overwrites live state.
    """
    manifest = read_backup_manifest(backup_dir)
    if destination_database_path.exists():
        raise InvalidInputError(
            f"restore destination {destination_database_path} already exists",
            details={"destination_database_path": str(destination_database_path)},
        )
    destination_database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_dir / _DATABASE_NAME, destination_database_path)

    backup_artifacts_dir = backup_dir / _ARTIFACTS_DIRNAME
    if manifest.includes_artifacts and backup_artifacts_dir.is_dir():
        if destination_artifact_store_root is None:
            raise InvalidInputError(
                "backup includes artifacts but no destination_artifact_store_root was provided"
            )
        shutil.copytree(backup_artifacts_dir, destination_artifact_store_root, dirs_exist_ok=True)
    return manifest


__all__ = ["BackupManifest", "backup_ledger", "read_backup_manifest", "restore_ledger"]
