"""The atomic, multi-aggregate command-append API.

One :func:`append` call is one SQLite transaction. Every event in an
:class:`AppendCommand` either commits together or none of them do: the
first :class:`~enginery.ledger.errors.ExpectedVersionConflictError` raised
while checking any event's expected aggregate version aborts the whole
command, rolling back every event already written earlier in the same
call. Later milestones extend :class:`AppendCommand` with artifact
metadata references, lease/process-manager updates, projection writes,
inbox acknowledgement, and outbox rows — all folded into this same
transaction rather than a second commit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.ledger.connection import transaction
from enginery.ledger.errors import ExpectedVersionConflictError


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


@dataclass(frozen=True, slots=True)
class EventWrite:
    """One aggregate transition to append within a command.

    ``expected_version`` is the aggregate version the caller last observed
    (``0`` for an aggregate that must not yet exist). ``payload`` is the
    already-serialized event body — typically a domain aggregate's
    ``*_to_dict`` envelope from ``enginery.domain.serialization`` — the
    ledger stores it opaquely and does not interpret aggregate-specific
    fields. ``causation_id`` defaults to the owning command's
    ``correlation_id`` when omitted, matching a root cause rather than a
    caused-by-another-event chain.
    """

    aggregate_type: str
    aggregate_id: str
    expected_version: int
    event_type: str
    schema_version: int
    payload: Mapping[str, object]
    causation_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_blank(self.aggregate_type, field_name="aggregate_type")
        _require_non_blank(self.aggregate_id, field_name="aggregate_id")
        _require_non_blank(self.event_type, field_name="event_type")
        if self.expected_version < 0:
            raise InvalidInputError(
                "expected_version cannot be negative",
                details={"expected_version": self.expected_version},
            )
        if self.schema_version < 1:
            raise InvalidInputError(
                "schema_version must be at least 1",
                details={"schema_version": self.schema_version},
            )


@dataclass(frozen=True, slots=True)
class AppendCommand:
    """One atomic unit of ledger work: at least one event, one shared
    ``correlation_id`` binding every event and side record this command
    produces."""

    correlation_id: str
    events: tuple[EventWrite, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_blank(self.correlation_id, field_name="correlation_id")
        if not self.events:
            raise InvalidInputError("an AppendCommand must write at least one event")


@dataclass(frozen=True, slots=True)
class AppendedEvent:
    """The durable identity assigned to one written event: its global
    commit sequence and its aggregate's new version."""

    aggregate_type: str
    aggregate_id: str
    commit_seq: int
    aggregate_version: int


@dataclass(frozen=True, slots=True)
class AppendResult:
    correlation_id: str
    events: tuple[AppendedEvent, ...]


def _current_aggregate_version(
    connection: sqlite3.Connection, *, aggregate_type: str, aggregate_id: str
) -> int:
    row = connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = ? AND aggregate_id = ?",
        (aggregate_type, aggregate_id),
    ).fetchone()
    return int(row["version"]) if row is not None else 0


def append(connection: sqlite3.Connection, command: AppendCommand) -> AppendResult:
    """Append every event in ``command`` inside one transaction.

    Raises :class:`ExpectedVersionConflictError` without writing anything
    if any event's ``expected_version`` is stale by the time its turn is
    checked; SQLite's transaction rollback on the raised exception
    guarantees earlier writes in the same command are undone too.
    """
    appended: list[AppendedEvent] = []
    with transaction(connection):
        for event in command.events:
            current_version = _current_aggregate_version(
                connection,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
            )
            if current_version != event.expected_version:
                raise ExpectedVersionConflictError(
                    f"expected version {event.expected_version} for "
                    f"{event.aggregate_type}:{event.aggregate_id}, found {current_version}",
                    details={
                        "aggregate_type": event.aggregate_type,
                        "aggregate_id": event.aggregate_id,
                        "expected_version": event.expected_version,
                        "actual_version": current_version,
                    },
                )
            new_version = current_version + 1
            causation_id = event.causation_id or command.correlation_id
            payload_json = json.dumps(dict(event.payload), sort_keys=True, separators=(",", ":"))
            cursor = connection.execute(
                """
                INSERT INTO events (
                    aggregate_type, aggregate_id, aggregate_version, event_type,
                    schema_version, payload, correlation_id, causation_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.aggregate_type,
                    event.aggregate_id,
                    new_version,
                    event.event_type,
                    event.schema_version,
                    payload_json,
                    command.correlation_id,
                    causation_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO aggregates (aggregate_type, aggregate_id, version)
                VALUES (?, ?, ?)
                ON CONFLICT (aggregate_type, aggregate_id)
                DO UPDATE SET version = excluded.version
                """,
                (event.aggregate_type, event.aggregate_id, new_version),
            )
            commit_seq = cursor.lastrowid
            assert commit_seq is not None
            appended.append(
                AppendedEvent(
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    commit_seq=commit_seq,
                    aggregate_version=new_version,
                )
            )
    return AppendResult(correlation_id=command.correlation_id, events=tuple(appended))


__all__ = ["AppendCommand", "AppendResult", "AppendedEvent", "EventWrite", "append"]
