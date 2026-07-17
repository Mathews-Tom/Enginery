"""SQLite connection lifecycle: pragmas, transaction control.

Every ledger connection is opened with the same pragmas so crash-safety
properties (WAL durability, foreign-key enforcement, busy-wait instead of
immediate lock failure) hold identically across the CLI, tests, and the
fault-injection harness. ``isolation_level=None`` puts the connection in
autocommit mode so :func:`transaction` has exclusive, explicit control over
transaction boundaries — no statement silently opens an implicit
transaction outside a caller's ``with transaction(conn):`` block.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_MS = 5_000


def open_connection(database_path: Path) -> sqlite3.Connection:
    """Open one SQLite connection configured for crash-safe ledger use.

    ``WAL`` journal mode gives readers a consistent snapshot without
    blocking the single writer, and survives a hard process kill: on next
    open, SQLite replays or discards the WAL exactly as it would a
    rollback journal. ``foreign_keys`` is enabled because migrations
    declare referential integrity between events, projections, and
    process-manager state.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(database_path),
        isolation_level=None,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = FULL")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run one ``BEGIN IMMEDIATE`` transaction, committing on clean exit.

    ``BEGIN IMMEDIATE`` acquires the write lock up front rather than on
    the first write statement, so two concurrent writers fail fast with
    ``sqlite3.OperationalError`` instead of one silently upgrading mid
    transaction and risking a deadlock. Any exception raised inside the
    block — including a domain-level :class:`ExpectedVersionConflictError`
    raised deliberately after some statements already ran — rolls back
    every statement executed since ``BEGIN IMMEDIATE``, so a multi-
    aggregate command never partially commits.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


__all__ = ["open_connection", "transaction"]
