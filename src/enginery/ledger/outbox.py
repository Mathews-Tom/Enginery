"""Transactional outbox: durable intake for eventual external projections.

Rows are written only inside the same transaction as the events that
produced them — :func:`write_entry` assumes a caller-owned transaction, the
same contract as :func:`enginery.ledger.inbox.acknowledge_command`. A
separate consumer (outside this milestone's scope: "external outbox
consumers" is explicitly deferred) later reads pending rows and marks them
dispatched; external calls never run inside a ledger transaction, so
dispatch confirmation is always a second, later write.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.ledger.connection import transaction
from enginery.ledger.redaction import assert_mapping_has_no_raw_credentials


@dataclass(frozen=True, slots=True)
class OutboxWrite:
    target: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise InvalidInputError("target must be a non-blank string")


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    outbox_id: int
    correlation_id: str
    target: str
    status: str
    created_at: str
    dispatched_at: str | None


def _row_to_record(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=row["outbox_id"],
        correlation_id=row["correlation_id"],
        target=row["target"],
        status=row["status"],
        created_at=row["created_at"],
        dispatched_at=row["dispatched_at"],
    )


def write_entry(connection: sqlite3.Connection, *, correlation_id: str, entry: OutboxWrite) -> int:
    """Insert one pending outbox row. Assumes a caller-owned transaction."""
    assert_mapping_has_no_raw_credentials(entry.payload)
    payload_json = json.dumps(dict(entry.payload), sort_keys=True, separators=(",", ":"))
    cursor = connection.execute(
        """
        INSERT INTO outbox (correlation_id, target, payload, created_at, dispatched_at, status)
        VALUES (?, ?, ?, ?, NULL, 'pending')
        """,
        (correlation_id, entry.target, payload_json, datetime.now(UTC).isoformat()),
    )
    outbox_id = cursor.lastrowid
    assert outbox_id is not None
    return outbox_id


def list_pending(connection: sqlite3.Connection, *, limit: int = 100) -> tuple[OutboxRecord, ...]:
    rows = connection.execute(
        "SELECT * FROM outbox WHERE status = 'pending' ORDER BY outbox_id LIMIT ?",
        (limit,),
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def mark_dispatched(connection: sqlite3.Connection, outbox_id: int) -> None:
    """Standalone write: dispatch confirmation happens after the external
    call, outside any ledger command transaction, so it opens its own."""
    with transaction(connection):
        connection.execute(
            "UPDATE outbox SET status = 'dispatched', dispatched_at = ? WHERE outbox_id = ?",
            (datetime.now(UTC).isoformat(), outbox_id),
        )


__all__ = ["OutboxRecord", "OutboxWrite", "list_pending", "mark_dispatched", "write_entry"]
