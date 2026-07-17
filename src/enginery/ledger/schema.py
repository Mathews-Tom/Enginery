"""SQLite DDL for every ledger migration, in application order.

Migrations are forward-only and strictly additive. A schema mistake is
corrected by a new migration appended to :data:`MIGRATIONS`, never by
editing an already-applied one — matching the design rule that historical
events are never rewritten in place. ``scripts/generate_ledger_schema_doc.py``
renders this module into ``docs/ledger-schema.md`` so the schema
documentation cannot silently drift from the code that defines it.

Each :class:`Migration` lists its statements as individually executable
SQL strings rather than one multi-statement script: the migration runner
executes them one at a time inside its own explicit transaction, and
``sqlite3`` cannot split a multi-statement script without an implicit
commit that would defeat that transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward migration: a version, a human description, and the
    ordered DDL statements that bring the schema from ``version - 1`` (in
    application order) to ``version``."""

    version: int
    description: str
    statements: tuple[str, ...]


_MIGRATION_0001_LEDGER_CORE = Migration(
    version=1,
    description="ledger core: schema_migrations, aggregates, events",
    statements=(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE aggregates (
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (aggregate_type, aggregate_id)
        )
        """,
        """
        CREATE TABLE events (
            commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE (aggregate_type, aggregate_id, aggregate_version)
        )
        """,
        "CREATE INDEX events_correlation_idx ON events (correlation_id)",
        "CREATE INDEX events_aggregate_idx ON events (aggregate_type, aggregate_id)",
    ),
)

_MIGRATION_0002_INBOX_OUTBOX_PROCESS_MANAGER = Migration(
    version=2,
    description="command inbox, transactional outbox, process-manager state, node leases",
    statements=(
        """
        CREATE TABLE command_inbox (
            command_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            command_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'rejected')),
            processed_at TEXT
        )
        """,
        """
        CREATE TABLE outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id TEXT NOT NULL,
            target TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            dispatched_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'failed'))
        )
        """,
        "CREATE INDEX outbox_status_idx ON outbox (status)",
        """
        CREATE TABLE process_manager_state (
            process_manager_name TEXT NOT NULL,
            state_key TEXT NOT NULL,
            state_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (process_manager_name, state_key)
        )
        """,
        """
        CREATE TABLE node_leases (
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            fencing_token INTEGER NOT NULL,
            owner TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            expires_at TEXT,
            PRIMARY KEY (run_id, node_id)
        )
        """,
    ),
)

MIGRATIONS: tuple[Migration, ...] = (
    _MIGRATION_0001_LEDGER_CORE,
    _MIGRATION_0002_INBOX_OUTBOX_PROCESS_MANAGER,
)

__all__ = ["MIGRATIONS", "Migration"]
