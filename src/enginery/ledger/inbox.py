"""Command inbox: durable intake for mutating operator/CLI commands.

A mutating command is enqueued here as ``pending`` independently of
whatever transaction later processes it — the coordinator that consumes
pending rows is a later milestone. ``events.append`` acknowledges a
processed inbox row as part of the same transaction that writes the
resulting events, satisfying the one-transaction-per-command contract's
"inbox acknowledgement" clause. This module owns enqueue and read;
:func:`acknowledge_command` is the transaction-scoped primitive
``events.append`` calls, not a standalone commit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.ledger.connection import transaction
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.redaction import assert_mapping_has_no_raw_credentials


@dataclass(frozen=True, slots=True)
class InboxRecord:
    command_id: str
    idempotency_key: str | None
    command_type: str
    correlation_id: str
    status: str
    received_at: str
    processed_at: str | None


def _row_to_record(row: sqlite3.Row) -> InboxRecord:
    return InboxRecord(
        command_id=row["command_id"],
        idempotency_key=row["idempotency_key"],
        command_type=row["command_type"],
        correlation_id=row["correlation_id"],
        status=row["status"],
        received_at=row["received_at"],
        processed_at=row["processed_at"],
    )


def read_command(connection: sqlite3.Connection, command_id: str) -> InboxRecord | None:
    row = connection.execute(
        "SELECT * FROM command_inbox WHERE command_id = ?", (command_id,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def find_by_idempotency_key(
    connection: sqlite3.Connection, idempotency_key: str
) -> InboxRecord | None:
    row = connection.execute(
        "SELECT * FROM command_inbox WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def enqueue_command(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    command_type: str,
    correlation_id: str,
    payload: Mapping[str, object],
    idempotency_key: str | None = None,
) -> InboxRecord:
    """Enqueue one pending command.

    A repeated call with the same ``idempotency_key`` returns the
    existing record unchanged rather than inserting a duplicate or
    raising a uniqueness violation — this is what makes the CLI's
    documented idempotency-key contract actually idempotent.
    """
    if not command_id.strip():
        raise InvalidInputError("command_id must be a non-blank string")
    if not command_type.strip():
        raise InvalidInputError("command_type must be a non-blank string")
    if not correlation_id.strip():
        raise InvalidInputError("correlation_id must be a non-blank string")

    if idempotency_key is not None:
        existing = find_by_idempotency_key(connection, idempotency_key)
        if existing is not None:
            return existing

    assert_mapping_has_no_raw_credentials(payload)
    received_at = datetime.now(UTC).isoformat()
    payload_json = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO command_inbox (
                command_id, idempotency_key, command_type, payload,
                correlation_id, received_at, status, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)
            """,
            (
                command_id,
                idempotency_key,
                command_type,
                payload_json,
                correlation_id,
                received_at,
            ),
        )
    return InboxRecord(
        command_id=command_id,
        idempotency_key=idempotency_key,
        command_type=command_type,
        correlation_id=correlation_id,
        status="pending",
        received_at=received_at,
        processed_at=None,
    )


def acknowledge_command(connection: sqlite3.Connection, command_id: str) -> None:
    """Mark ``command_id`` processed. Must run inside a caller-owned
    transaction (:func:`enginery.ledger.events.append`) rather than
    opening its own, so acknowledgement commits atomically with the
    events, projections, and outbox rows the command produced.

    Raises :class:`ExpectedVersionConflictError` if the command is
    unknown or was already processed or rejected — acknowledging the
    same durable pointer twice indicates a race the caller must not
    silently ignore.
    """
    row = connection.execute(
        "SELECT status FROM command_inbox WHERE command_id = ?", (command_id,)
    ).fetchone()
    if row is None:
        raise ExpectedVersionConflictError(
            f"cannot acknowledge unknown inbox command {command_id!r}",
            details={"command_id": command_id},
        )
    if row["status"] != "pending":
        raise ExpectedVersionConflictError(
            f"inbox command {command_id!r} is already {row['status']!r}, not pending",
            details={"command_id": command_id, "status": row["status"]},
        )
    connection.execute(
        "UPDATE command_inbox SET status = 'processed', processed_at = ? WHERE command_id = ?",
        (datetime.now(UTC).isoformat(), command_id),
    )


__all__ = [
    "InboxRecord",
    "acknowledge_command",
    "enqueue_command",
    "find_by_idempotency_key",
    "read_command",
]
