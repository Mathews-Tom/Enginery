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
from enginery.ledger.errors import SchemaVersionUnsupportedError

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
    return ProjectionRecord(
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        aggregate_version=row["aggregate_version"],
        event_type=row["event_type"],
        schema_version=row["schema_version"],
        state=json.loads(row["state_json"]),
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
        latest_versions = connection.execute(
            "SELECT aggregate_type, aggregate_id, MAX(aggregate_version) AS aggregate_version "
            "FROM events GROUP BY aggregate_type, aggregate_id"
        ).fetchall()
        for row in latest_versions:
            event_row = connection.execute(
                "SELECT event_type, schema_version, payload FROM events "
                "WHERE aggregate_type = ? AND aggregate_id = ? AND aggregate_version = ?",
                (row["aggregate_type"], row["aggregate_id"], row["aggregate_version"]),
            ).fetchone()
            if event_row["schema_version"] > max_supported_schema_version:
                raise SchemaVersionUnsupportedError(
                    f"event schema version {event_row['schema_version']} for "
                    f"{row['aggregate_type']}:{row['aggregate_id']} exceeds the maximum "
                    f"supported version {max_supported_schema_version}",
                    details={
                        "aggregate_type": row["aggregate_type"],
                        "aggregate_id": row["aggregate_id"],
                        "schema_version": event_row["schema_version"],
                        "max_supported_schema_version": max_supported_schema_version,
                    },
                )
            apply_projection_update(
                connection,
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                aggregate_version=row["aggregate_version"],
                event_type=event_row["event_type"],
                schema_version=event_row["schema_version"],
                payload_json=event_row["payload"],
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
