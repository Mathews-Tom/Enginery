"""``enginery ledger``: consistency, backup, restore, and replay commands.

These are read-mostly operator commands over an existing ledger file —
none of them execute workflows. ``verify`` and ``rebuild-projections``
open the database directly rather than through
:class:`~enginery.ledger.service.LedgerService`, so neither silently
applies a pending migration; a stale schema is reported (``verify``) or
left for the operator to migrate first, matching "no silent migration."
``backup`` and ``restore`` copy files but never mutate a live ledger's
own tables in place.
"""

from __future__ import annotations

from pathlib import Path

from enginery.ledger.backup import BackupManifest, backup_ledger, restore_ledger
from enginery.ledger.connection import open_connection
from enginery.ledger.projections import RebuildReport, rebuild_projections
from enginery.ledger.verify import VerificationReport, verify_ledger


def run_verify(*, database: Path, artifacts: Path | None) -> VerificationReport:
    return verify_ledger(database, artifact_store_root=artifacts)


def run_backup(*, database: Path, output: Path, artifacts: Path | None) -> BackupManifest:
    return backup_ledger(database, output, artifact_store_root=artifacts)


def run_restore(*, backup: Path, database: Path, artifacts: Path | None) -> BackupManifest:
    return restore_ledger(backup, database, destination_artifact_store_root=artifacts)


def run_rebuild_projections(*, database: Path) -> RebuildReport:
    connection = open_connection(database)
    try:
        return rebuild_projections(connection)
    finally:
        connection.close()


__all__ = ["run_backup", "run_rebuild_projections", "run_restore", "run_verify"]
