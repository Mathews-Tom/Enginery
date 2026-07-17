"""Forward-only migration runner.

Each migration runs inside its own ``BEGIN IMMEDIATE`` transaction and is
recorded in ``schema_migrations`` only after every one of its statements
succeeds. There is no catch-and-continue: the first failing migration
raises immediately, its transaction rolls back in full, and no later
migration is attempted — the ledger stays pinned at its last known-good
schema version rather than silently skipping ahead over a broken step.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from enginery.ledger.connection import transaction
from enginery.ledger.errors import MigrationFailedError
from enginery.ledger.schema import MIGRATIONS, Migration


def current_schema_version(connection: sqlite3.Connection) -> int:
    """The highest applied migration version, or ``0`` for a fresh database."""
    table_exists = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table_exists is None:
        return 0
    row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    version = row["version"]
    return int(version) if version is not None else 0


def apply_pending_migrations(
    connection: sqlite3.Connection, *, migrations: tuple[Migration, ...] = MIGRATIONS
) -> tuple[int, ...]:
    """Apply every migration whose version exceeds the current schema
    version, in ascending order. Returns the versions actually applied.

    Raises :class:`MigrationFailedError` and stops at the first failure.
    A caller that receives this exception must not proceed to serve the
    ledger — the database remains exactly as it was before this call.
    """
    applied: list[int] = []
    current = current_schema_version(connection)
    pending = sorted(
        (migration for migration in migrations if migration.version > current),
        key=lambda migration: migration.version,
    )
    for migration in pending:
        try:
            with transaction(connection):
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.description, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as error:
            raise MigrationFailedError(
                f"migration {migration.version} ({migration.description!r}) failed: {error}",
                details={"version": migration.version, "description": migration.description},
            ) from error
        applied.append(migration.version)
    return tuple(applied)


__all__ = ["apply_pending_migrations", "current_schema_version"]
