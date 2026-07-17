"""Latest-state projections, kept transactionally in sync with events.

A projection row is the current-state snapshot for one aggregate:
because every stored event payload is already the full serialized
aggregate state after its transition (not a delta), "current state" is
exactly the highest-``aggregate_version`` event's payload. This makes
projection maintenance a plain upsert on every append and projection
*rebuild* a deterministic replay keyed by each aggregate's own version
sequence — "event order is total per aggregate" is what the design
requires, not a global ordering across aggregates.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.ledger.connection import transaction
from enginery.ledger.errors import CorruptedEventError, SchemaVersionUnsupportedError

CURRENT_MAX_SUPPORTED_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    schema_version: int
    state: Mapping[str, object]
    updated_at: str


@dataclass(frozen=True, slots=True)
class RebuildReport:
    aggregates_rebuilt: int


def apply_projection_update(
    connection: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    event_type: str,
    schema_version: int,
    payload_json: str,
) -> None:
    """Upsert one aggregate's projection row to its latest state. Assumes a
    caller-owned transaction; called from :func:`enginery.ledger.events.append`
    once per written event."""
    connection.execute(
        """
        INSERT INTO projections (
            aggregate_type, aggregate_id, aggregate_version, event_type,
            schema_version, state_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (aggregate_type, aggregate_id)
        DO UPDATE SET aggregate_version = excluded.aggregate_version,
                       event_type = excluded.event_type,
                       schema_version = excluded.schema_version,
                       state_json = excluded.state_json,
                       updated_at = excluded.updated_at
        """,
        (
            aggregate_type,
            aggregate_id,
            aggregate_version,
            event_type,
            schema_version,
            payload_json,
            datetime.now(UTC).isoformat(),
        ),
    )


def read_projection(
    connection: sqlite3.Connection, *, aggregate_type: str, aggregate_id: str
) -> ProjectionRecord | None:
    row = connection.execute(
        "SELECT * FROM projections WHERE aggregate_type = ? AND aggregate_id = ?",
        (aggregate_type, aggregate_id),
    ).fetchone()
    if row is None:
        return None
    try:
        state = json.loads(row["state_json"])
    except json.JSONDecodeError as error:
        raise CorruptedEventError(
            f"projection state for {aggregate_type}:{aggregate_id} is not valid JSON: {error}",
            details={"aggregate_type": aggregate_type, "aggregate_id": aggregate_id},
        ) from error
    return ProjectionRecord(
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        event_type=row["event_type"],
        schema_version=row["schema_version"],
        state=state,
        updated_at=row["updated_at"],
    )


def rebuild_projections(
    connection: sqlite3.Connection,
    *,
    max_supported_schema_version: int = CURRENT_MAX_SUPPORTED_SCHEMA_VERSION,
) -> RebuildReport:
    """Deterministically rebuild every projection from stored events.

    Replaces the entire ``projections`` table inside one transaction: an
    unsupported event schema version stops the rebuild and rolls back,
    leaving the previous projections intact rather than half-replaced.
    Each aggregate's projection is derived independently from its own
    highest ``aggregate_version`` event — replay does not depend on the
    global commit sequence or on any other aggregate's history.
    """
    rebuilt = 0
    with transaction(connection):
        connection.execute("DELETE FROM projections")
        latest_events = connection.execute(
            """
            SELECT e.aggregate_type, e.aggregate_id, e.aggregate_version,
                   e.event_type, e.schema_version, e.payload
            FROM events e
            INNER JOIN (
                SELECT aggregate_type, aggregate_id, MAX(aggregate_version) AS aggregate_version
                FROM events
                GROUP BY aggregate_type, aggregate_id
            ) latest
            ON e.aggregate_type = latest.aggregate_type
            AND e.aggregate_id = latest.aggregate_id
            AND e.aggregate_version = latest.aggregate_version
            """
        ).fetchall()
        for row in latest_events:
            if row["schema_version"] > max_supported_schema_version:
                raise SchemaVersionUnsupportedError(
                    f"event schema version {row['schema_version']} for "
                    f"{row['aggregate_type']}:{row['aggregate_id']} exceeds the maximum "
                    f"supported version {max_supported_schema_version}",
                    details={
                        "aggregate_type": row["aggregate_type"],
                        "aggregate_id": row["aggregate_id"],
                        "schema_version": row["schema_version"],
                        "max_supported_schema_version": max_supported_schema_version,
                    },
                )
            try:
                json.loads(row["payload"])
            except json.JSONDecodeError as error:
                raise CorruptedEventError(
                    f"event payload for {row['aggregate_type']}:{row['aggregate_id']} at "
                    f"version {row['aggregate_version']} is not valid JSON: {error}",
                    details={
                        "aggregate_type": row["aggregate_type"],
                        "aggregate_id": row["aggregate_id"],
                        "aggregate_version": row["aggregate_version"],
                    },
                ) from error
            apply_projection_update(
                connection,
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                aggregate_version=row["aggregate_version"],
                event_type=row["event_type"],
                schema_version=row["schema_version"],
                payload_json=row["payload"],
            )
            rebuilt += 1
    return RebuildReport(aggregates_rebuilt=rebuilt)


__all__ = [
    "CURRENT_MAX_SUPPORTED_SCHEMA_VERSION",
    "ProjectionRecord",
    "RebuildReport",
    "apply_projection_update",
    "read_projection",
    "rebuild_projections",
]
