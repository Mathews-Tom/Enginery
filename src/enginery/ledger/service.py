"""``LedgerService``: the ledger's single write/read facade.

Wraps one SQLite connection to one ledger file, applying every pending
migration at open time and exposing the atomic command-append API. Later
layers depend on this facade rather than opening SQLite connections
themselves, so the crash-safety and transaction-boundary guarantees stay
in one place as the append transaction grows across later milestones.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.artifacts import ArtifactMetadataRecord
from enginery.ledger.artifacts import read_artifact_metadata as _read_artifact_metadata
from enginery.ledger.commit_cursors import advance_cursor as _advance_cursor
from enginery.ledger.commit_cursors import read_cursor as _read_cursor
from enginery.ledger.connection import open_connection
from enginery.ledger.events import AppendCommand, AppendResult, append
from enginery.ledger.inbox import InboxRecord
from enginery.ledger.inbox import enqueue_command as _enqueue_command
from enginery.ledger.inbox import find_by_idempotency_key as _find_by_idempotency_key
from enginery.ledger.inbox import read_command as _read_inbox_command
from enginery.ledger.leases import LeaseRecord
from enginery.ledger.leases import read_lease as _read_lease
from enginery.ledger.migrations import apply_pending_migrations, current_schema_version
from enginery.ledger.outbox import OutboxRecord
from enginery.ledger.outbox import list_pending as _list_pending_outbox
from enginery.ledger.outbox import mark_dispatched as _mark_outbox_dispatched
from enginery.ledger.process_manager import ProcessManagerStateRecord
from enginery.ledger.process_manager import read_process_manager_state as _read_pm_state
from enginery.ledger.projections import ProjectionRecord, RebuildReport
from enginery.ledger.projections import read_projection as _read_projection
from enginery.ledger.projections import rebuild_projections as _rebuild_projections


class LedgerService:
    """A migrated SQLite ledger and its command-append API."""

    def __init__(
        self, connection: sqlite3.Connection, *, artifact_store: ArtifactStore | None = None
    ) -> None:
        self._connection = connection
        self._artifact_store = artifact_store

    @classmethod
    def open(cls, database_path: Path, *, artifact_store_root: Path | None = None) -> LedgerService:
        """Open ``database_path``, applying every pending migration first.

        Raises before returning a usable service if any migration fails —
        a caller must never receive a partially migrated ledger, matching
        the "interrupted migration does not start the application"
        acceptance criterion. ``artifact_store_root``, when given, roots
        an :class:`~enginery.ledger.artifact_store.ArtifactStore`
        alongside the ledger so ``append`` can accept
        ``artifact_references``.
        """
        connection = open_connection(database_path)
        apply_pending_migrations(connection)
        artifact_store = (
            ArtifactStore(artifact_store_root) if artifact_store_root is not None else None
        )
        return cls(connection, artifact_store=artifact_store)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def artifact_store(self) -> ArtifactStore | None:
        return self._artifact_store

    @property
    def schema_version(self) -> int:
        return current_schema_version(self._connection)

    def append(self, command: AppendCommand) -> AppendResult:
        return append(self._connection, command, artifact_store=self._artifact_store)

    def publish_artifact_bytes(
        self, data: bytes, *, media_type: str = "application/octet-stream"
    ) -> Digest:
        if self._artifact_store is None:
            raise InvalidInputError("no artifact_store configured for this LedgerService")
        return self._artifact_store.publish_bytes(data, media_type=media_type)

    def read_artifact_metadata(self, artifact_id: str) -> ArtifactMetadataRecord | None:
        return _read_artifact_metadata(self._connection, artifact_id)

    def enqueue_command(
        self,
        *,
        command_id: str,
        command_type: str,
        correlation_id: str,
        payload: Mapping[str, object],
        idempotency_key: str | None = None,
    ) -> InboxRecord:
        return _enqueue_command(
            self._connection,
            command_id=command_id,
            command_type=command_type,
            correlation_id=correlation_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def read_inbox_command(self, command_id: str) -> InboxRecord | None:
        return _read_inbox_command(self._connection, command_id)

    def find_inbox_command_by_idempotency_key(self, idempotency_key: str) -> InboxRecord | None:
        return _find_by_idempotency_key(self._connection, idempotency_key)

    def list_pending_outbox(self, *, limit: int = 100) -> tuple[OutboxRecord, ...]:
        return _list_pending_outbox(self._connection, limit=limit)

    def mark_outbox_dispatched(self, outbox_id: int) -> None:
        _mark_outbox_dispatched(self._connection, outbox_id)

    def read_process_manager_state(
        self, *, process_manager_name: str, state_key: str
    ) -> ProcessManagerStateRecord | None:
        return _read_pm_state(
            self._connection, process_manager_name=process_manager_name, state_key=state_key
        )

    def read_lease(self, *, run_id: str, node_id: str) -> LeaseRecord | None:
        return _read_lease(self._connection, run_id=run_id, node_id=node_id)

    def read_projection(self, *, aggregate_type: str, aggregate_id: str) -> ProjectionRecord | None:
        return _read_projection(
            self._connection, aggregate_type=aggregate_type, aggregate_id=aggregate_id
        )

    def rebuild_projections(self) -> RebuildReport:
        return _rebuild_projections(self._connection)

    def read_cursor(self, consumer_name: str) -> int:
        return _read_cursor(self._connection, consumer_name)

    def advance_cursor(self, consumer_name: str, commit_seq: int, *, force: bool = False) -> None:
        _advance_cursor(self._connection, consumer_name, commit_seq, force=force)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> LedgerService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = ["LedgerService"]
