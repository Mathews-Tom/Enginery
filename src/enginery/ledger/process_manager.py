"""Process-manager state: durable, versioned key/value state for process
managers reacting to ledger events.

No process manager exists in this milestone — the coordinator and
scheduler that consume this state are M5. M3 owns exactly the schema and
the optimistic-concurrency write contract so those consumers only need to
learn one API rather than inventing their own persistence.

State is namespaced by ``process_manager_name`` (which process manager)
and ``state_key`` (which instance of that process manager's state, for
example a run ID) — the same optimistic-concurrency shape as aggregate
events, so :func:`enginery.ledger.events.append` can fold a stale-version
process-manager write into the same command-wide rollback.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.ledger.errors import ExpectedVersionConflictError


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


@dataclass(frozen=True, slots=True)
class ProcessManagerStateWrite:
    process_manager_name: str
    state_key: str
    expected_version: int
    state: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_non_blank(self.process_manager_name, field_name="process_manager_name")
        _require_non_blank(self.state_key, field_name="state_key")
        if self.expected_version < 0:
            raise InvalidInputError(
                "expected_version cannot be negative",
                details={"expected_version": self.expected_version},
            )


@dataclass(frozen=True, slots=True)
class ProcessManagerStateRecord:
    process_manager_name: str
    state_key: str
    state_version: int
    state: Mapping[str, object]
    updated_at: str


def _current_version(
    connection: sqlite3.Connection, *, process_manager_name: str, state_key: str
) -> int:
    row = connection.execute(
        "SELECT state_version FROM process_manager_state "
        "WHERE process_manager_name = ? AND state_key = ?",
        (process_manager_name, state_key),
    ).fetchone()
    return int(row["state_version"]) if row is not None else 0


def apply_process_manager_update(
    connection: sqlite3.Connection, write: ProcessManagerStateWrite
) -> int:
    """Upsert one process manager's keyed state. Assumes a caller-owned
    transaction. Raises :class:`ExpectedVersionConflictError` — folded
    into the caller's rollback — if ``expected_version`` is stale."""
    current_version = _current_version(
        connection,
        process_manager_name=write.process_manager_name,
        state_key=write.state_key,
    )
    if current_version != write.expected_version:
        raise ExpectedVersionConflictError(
            f"expected process-manager version {write.expected_version} for "
            f"{write.process_manager_name}:{write.state_key}, found {current_version}",
            details={
                "process_manager_name": write.process_manager_name,
                "state_key": write.state_key,
                "expected_version": write.expected_version,
                "actual_version": current_version,
            },
        )
    new_version = current_version + 1
    state_json = json.dumps(dict(write.state), sort_keys=True, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO process_manager_state (
            process_manager_name, state_key, state_version, state_json, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (process_manager_name, state_key)
        DO UPDATE SET state_version = excluded.state_version,
                       state_json = excluded.state_json,
                       updated_at = excluded.updated_at
        """,
        (
            write.process_manager_name,
            write.state_key,
            new_version,
            state_json,
            datetime.now(UTC).isoformat(),
        ),
    )
    return new_version


def read_process_manager_state(
    connection: sqlite3.Connection, *, process_manager_name: str, state_key: str
) -> ProcessManagerStateRecord | None:
    row = connection.execute(
        "SELECT * FROM process_manager_state WHERE process_manager_name = ? AND state_key = ?",
        (process_manager_name, state_key),
    ).fetchone()
    if row is None:
        return None
    return ProcessManagerStateRecord(
        process_manager_name=row["process_manager_name"],
        state_key=row["state_key"],
        state_version=row["state_version"],
        state=json.loads(row["state_json"]),
        updated_at=row["updated_at"],
    )


__all__ = [
    "ProcessManagerStateRecord",
    "ProcessManagerStateWrite",
    "apply_process_manager_update",
    "read_process_manager_state",
]
