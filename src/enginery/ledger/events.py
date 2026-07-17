"""The atomic, multi-aggregate command-append API.

One :func:`append` call is one SQLite transaction. Every event in an
:class:`AppendCommand` either commits together or none of them do: the
first :class:`~enginery.ledger.errors.ExpectedVersionConflictError` raised
while checking any event's expected aggregate version aborts the whole
command, rolling back every event already written earlier in the same
call. :class:`AppendCommand` also carries inbox acknowledgement, outbox
entries, process-manager state updates, node-lease updates, and content-
addressed artifact metadata references — all folded into this same
transaction, satisfying the one-transaction-per-command contract for
expected aggregate versions, events, artifact metadata references,
lease/scheduling updates, process-manager updates, inbox acknowledgement,
and outbox rows. Every event payload is scanned for credential-shaped
content before it is written, matching "raw harness/provider payloads
cannot enter the ledger before adapter-side normalization/redaction."
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.artifacts import ArtifactMetadataWrite, apply_artifact_metadata
from enginery.ledger.connection import transaction
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.inbox import acknowledge_command
from enginery.ledger.leases import LeaseWrite, apply_lease_update
from enginery.ledger.outbox import OutboxWrite, write_entry
from enginery.ledger.process_manager import ProcessManagerStateWrite, apply_process_manager_update
from enginery.ledger.projections import apply_projection_update
from enginery.ledger.redaction import assert_mapping_has_no_raw_credentials


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
    produces.

    ``inbox_command_id``, when set, acknowledges that pending
    :mod:`enginery.ledger.inbox` row as ``processed`` inside this same
    transaction. ``outbox_entries``, ``process_manager_updates``,
    ``lease_updates``, and ``artifact_references`` write their respective
    tables inside this same transaction too, so a caller building one
    command from one operator request never needs a second commit for its
    side effects. ``artifact_references`` requires ``append`` to be called
    with a non-``None`` ``artifact_store`` — the ledger only ever records
    metadata for bytes that store can prove are already published.
    """

    correlation_id: str
    events: tuple[EventWrite, ...] = field(default_factory=tuple)
    inbox_command_id: str | None = None
    outbox_entries: tuple[OutboxWrite, ...] = field(default_factory=tuple)
    process_manager_updates: tuple[ProcessManagerStateWrite, ...] = field(default_factory=tuple)
    lease_updates: tuple[LeaseWrite, ...] = field(default_factory=tuple)
    artifact_references: tuple[ArtifactMetadataWrite, ...] = field(default_factory=tuple)

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
class AppendedProcessManagerState:
    process_manager_name: str
    state_key: str
    state_version: int


@dataclass(frozen=True, slots=True)
class AppendResult:
    correlation_id: str
    events: tuple[AppendedEvent, ...]
    inbox_acknowledged: bool = False
    outbox_ids: tuple[int, ...] = field(default_factory=tuple)
    process_manager_states: tuple[AppendedProcessManagerState, ...] = field(default_factory=tuple)
    artifact_ids: tuple[str, ...] = field(default_factory=tuple)


def _current_aggregate_version(
    connection: sqlite3.Connection, *, aggregate_type: str, aggregate_id: str
) -> int:
    row = connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = ? AND aggregate_id = ?",
        (aggregate_type, aggregate_id),
    ).fetchone()
    return int(row["version"]) if row is not None else 0


def append(
    connection: sqlite3.Connection,
    command: AppendCommand,
    *,
    artifact_store: ArtifactStore | None = None,
) -> AppendResult:
    """Append every event in ``command``, plus its inbox acknowledgement,
    outbox entries, process-manager updates, lease updates, and artifact
    metadata references, inside one transaction.

    Raises :class:`ExpectedVersionConflictError` without writing anything
    if any event's or process-manager update's expected version is stale,
    or if ``inbox_command_id`` is unknown or already processed;
    :class:`~enginery.ledger.errors.RawCredentialDetectedError` if any
    event payload looks credential-shaped; and
    :class:`InvalidInputError` if ``command.artifact_references`` is
    non-empty but ``artifact_store`` was not provided. SQLite's
    transaction rollback on any raised exception guarantees every write
    already performed earlier in the same command is undone too.
    """
    if command.artifact_references and artifact_store is None:
        raise InvalidInputError(
            "command carries artifact_references but no artifact_store was provided"
        )

    appended: list[AppendedEvent] = []
    process_manager_results: list[AppendedProcessManagerState] = []
    outbox_ids: list[int] = []
    artifact_ids: list[str] = []
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
            assert_mapping_has_no_raw_credentials(event.payload)
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
            apply_projection_update(
                connection,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                aggregate_version=new_version,
                event_type=event.event_type,
                schema_version=event.schema_version,
                payload_json=payload_json,
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

        for pm_write in command.process_manager_updates:
            new_pm_version = apply_process_manager_update(connection, pm_write)
            process_manager_results.append(
                AppendedProcessManagerState(
                    process_manager_name=pm_write.process_manager_name,
                    state_key=pm_write.state_key,
                    state_version=new_pm_version,
                )
            )

        for lease_write in command.lease_updates:
            apply_lease_update(connection, lease_write)

        for outbox_write in command.outbox_entries:
            outbox_ids.append(
                write_entry(connection, correlation_id=command.correlation_id, entry=outbox_write)
            )

        for artifact_write in command.artifact_references:
            assert artifact_store is not None  # validated above
            apply_artifact_metadata(connection, artifact_write, store=artifact_store)
            artifact_ids.append(str(artifact_write.artifact_id))

        if command.inbox_command_id is not None:
            acknowledge_command(connection, command.inbox_command_id)

    return AppendResult(
        correlation_id=command.correlation_id,
        events=tuple(appended),
        inbox_acknowledged=command.inbox_command_id is not None,
        outbox_ids=tuple(outbox_ids),
        process_manager_states=tuple(process_manager_results),
        artifact_ids=tuple(artifact_ids),
    )


__all__ = [
    "AppendCommand",
    "AppendResult",
    "AppendedEvent",
    "AppendedProcessManagerState",
    "EventWrite",
    "append",
]
