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
    """Read and validate a backup's manifest and confirm its ledger snapshot is present.

    Every defect -- a missing directory, manifest or database file, unreadable or truncated
    files, malformed JSON, a missing field or a field of the wrong type -- is an
    :class:`InvalidInputError` that names the backup; none escapes as a raw OS or decode error.
    """
    manifest_path = backup_dir / _MANIFEST_NAME
    if not backup_dir.is_dir():
        raise InvalidInputError(
            f"backup {backup_dir} is not a directory", details={"backup": str(backup_dir)}
        )
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise InvalidInputError(
            f"backup {backup_dir} has no {_MANIFEST_NAME}", details={"backup": str(backup_dir)}
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise InvalidInputError(
            f"backup manifest {manifest_path} cannot be read: {error}",
            details={"backup": str(backup_dir)},
        ) from error
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise InvalidInputError(
            f"backup manifest {manifest_path} is not valid JSON: {error}",
            details={"backup": str(backup_dir)},
        ) from error
    if not isinstance(raw, dict):
        raise InvalidInputError(
            f"backup manifest {manifest_path} must be a JSON object",
            details={"backup": str(backup_dir)},
        )
    created_at = _manifest_field(raw, "created_at", str, manifest_path)
    source_database = _manifest_field(raw, "source_database", str, manifest_path)
    includes_artifacts = _manifest_field(raw, "includes_artifacts", bool, manifest_path)
    schema_version = _manifest_field(raw, "schema_version", int, manifest_path)
    if not (backup_dir / _DATABASE_NAME).is_file():
        raise InvalidInputError(
            f"backup {backup_dir} is missing its ledger snapshot {_DATABASE_NAME}",
            details={"backup": str(backup_dir)},
        )
    return BackupManifest(
        created_at=created_at,
        schema_version=schema_version,
        source_database=source_database,
        includes_artifacts=includes_artifacts,
    )


def _manifest_field[T](raw: dict[str, object], key: str, kind: type[T], path: Path) -> T:
    value = raw.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise InvalidInputError(
            f"backup manifest {path} field {key!r} is missing or not of type {kind.__name__}",
            details={"manifest": str(path)},
        )
    return value


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


def backup_database_path(backup_dir: Path) -> Path:
    """The ledger snapshot inside a backup directory."""
    return backup_dir / _DATABASE_NAME


def backup_artifacts_path(backup_dir: Path) -> Path:
    """The artifact-store copy inside a backup directory (present only if it was included)."""
    return backup_dir / _ARTIFACTS_DIRNAME


__all__ = [
    "BackupManifest",
    "backup_artifacts_path",
    "backup_database_path",
    "backup_ledger",
    "read_backup_manifest",
    "restore_ledger",
]
