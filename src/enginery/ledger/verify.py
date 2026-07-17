"""Ledger and artifact-store consistency checks.

``verify_ledger`` is the single entry point behind ``enginery ledger
verify``. It never mutates the ledger: a corruption finding is reported,
not repaired — repair is always an explicit operator action (restore from
backup, or a future targeted admin command), matching "no catch-and-
continue" for anything that touches durable state.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from enginery.domain.digests import Digest
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.connection import open_connection
from enginery.ledger.migrations import current_schema_version
from enginery.ledger.schema import MIGRATIONS


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    schema_version: int
    issues: tuple[VerificationIssue, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return not self.issues


def _check_integrity(connection: sqlite3.Connection) -> list[VerificationIssue]:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    messages = [row[0] for row in rows]
    if messages == ["ok"]:
        return []
    return [
        VerificationIssue(code="integrity_check_failed", detail=message) for message in messages
    ]


def _check_schema_version(connection: sqlite3.Connection) -> list[VerificationIssue]:
    latest = MIGRATIONS[-1].version
    current = current_schema_version(connection)
    if current != latest:
        return [
            VerificationIssue(
                code="schema_version_stale",
                detail=f"ledger is at schema version {current}, expected {latest}",
            )
        ]
    return []


def _check_artifacts(
    connection: sqlite3.Connection, store: ArtifactStore
) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    rows = connection.execute("SELECT artifact_id, digest FROM artifacts").fetchall()
    for row in rows:
        algorithm, _, hex_value = row["digest"].partition(":")
        try:
            digest = Digest(algorithm=algorithm, hex_value=hex_value)
        except Exception as error:  # any malformed digest is a finding, not a bug
            issues.append(
                VerificationIssue(
                    code="artifact_digest_malformed",
                    detail=f"artifact {row['artifact_id']}: {error}",
                )
            )
            continue
        if not store.verify(digest):
            issues.append(
                VerificationIssue(
                    code="artifact_bytes_missing_or_corrupted",
                    detail=f"artifact {row['artifact_id']} references digest {digest}",
                )
            )
    return issues


def verify_ledger(
    database_path: Path, *, artifact_store_root: Path | None = None
) -> VerificationReport:
    """Run every consistency check against ``database_path``.

    ``artifact_store_root``, when given, additionally verifies every
    artifact metadata row's digest resolves to intact bytes. A database
    file too corrupted to even open (for example a truncated or
    bit-flipped SQLite header) is reported as a ``database_unreadable``
    issue rather than raising — a doctor command must diagnose the worst
    case, not crash on it.
    """
    try:
        connection = open_connection(database_path)
    except sqlite3.DatabaseError as error:
        return VerificationReport(
            schema_version=0,
            issues=(VerificationIssue(code="database_unreadable", detail=str(error)),),
        )
    try:
        issues = [*_check_integrity(connection), *_check_schema_version(connection)]
        if artifact_store_root is not None:
            store = ArtifactStore(artifact_store_root)
            issues.extend(_check_artifacts(connection, store))
        return VerificationReport(
            schema_version=current_schema_version(connection), issues=tuple(issues)
        )
    finally:
        connection.close()


__all__ = ["VerificationIssue", "VerificationReport", "verify_ledger"]
