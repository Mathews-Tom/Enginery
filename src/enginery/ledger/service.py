"""``LedgerService``: the ledger's single write/read facade.

Wraps one SQLite connection to one ledger file, applying every pending
migration at open time and exposing the atomic command-append API. Later
layers depend on this facade rather than opening SQLite connections
themselves, so the crash-safety and transaction-boundary guarantees stay
in one place as the append transaction grows across later milestones.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from enginery.ledger.connection import open_connection
from enginery.ledger.events import AppendCommand, AppendResult, append
from enginery.ledger.migrations import apply_pending_migrations, current_schema_version


class LedgerService:
    """A migrated SQLite ledger and its command-append API."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, database_path: Path) -> LedgerService:
        """Open ``database_path``, applying every pending migration first.

        Raises before returning a usable service if any migration fails —
        a caller must never receive a partially migrated ledger, matching
        the "interrupted migration does not start the application"
        acceptance criterion.
        """
        connection = open_connection(database_path)
        apply_pending_migrations(connection)
        return cls(connection)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def schema_version(self) -> int:
        return current_schema_version(self._connection)

    def append(self, command: AppendCommand) -> AppendResult:
        return append(self._connection, command)

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
