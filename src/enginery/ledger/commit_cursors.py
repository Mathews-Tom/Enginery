"""Durable, per-consumer commit cursors over the global commit sequence.

A consumer (a JSONL watch stream, an outbox dispatcher, a projector) reads
events in ``commit_seq`` order and periodically records how far it has
read so it can resume after a restart without re-delivering everything
from the beginning. Cursor state is a plain integer pointer, not a
transaction participant in :func:`enginery.ledger.events.append` — nothing
about appending an event depends on any consumer's read progress.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.ledger.connection import transaction


def read_cursor(connection: sqlite3.Connection, consumer_name: str) -> int:
    """The last commit sequence ``consumer_name`` has read, or ``0`` if it
    has never advanced — meaning "read from the beginning"."""
    row = connection.execute(
        "SELECT last_commit_seq FROM commit_cursors WHERE consumer_name = ?", (consumer_name,)
    ).fetchone()
    return int(row["last_commit_seq"]) if row is not None else 0


def advance_cursor(
    connection: sqlite3.Connection,
    consumer_name: str,
    commit_seq: int,
    *,
    force: bool = False,
) -> None:
    """Move ``consumer_name``'s cursor forward to ``commit_seq``.

    Refuses to regress a cursor by default — an accidental smaller value
    would silently cause redelivery a caller likely did not intend.
    ``force=True`` allows a deliberate reset, for example after a restore
    that intentionally rewinds a consumer.
    """
    if commit_seq < 0:
        raise InvalidInputError("commit_seq cannot be negative", details={"commit_seq": commit_seq})
    current = read_cursor(connection, consumer_name)
    if commit_seq < current and not force:
        raise InvalidInputError(
            f"cursor {consumer_name!r} cannot regress from {current} to {commit_seq} "
            "without force=True",
            details={"consumer_name": consumer_name, "current": current, "requested": commit_seq},
        )
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO commit_cursors (consumer_name, last_commit_seq, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (consumer_name)
            DO UPDATE SET last_commit_seq = excluded.last_commit_seq,
                           updated_at = excluded.updated_at
            """,
            (consumer_name, commit_seq, datetime.now(UTC).isoformat()),
        )


__all__ = ["advance_cursor", "read_cursor"]
