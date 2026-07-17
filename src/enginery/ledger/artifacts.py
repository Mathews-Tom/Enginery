"""Artifact metadata: durable pointers to content-addressed bytes.

Uses the real :class:`enginery.domain.artifact.Artifact` field vocabulary
(``ArtifactKind``, ``RedactionClassification``, and the dedicated ID
types) directly rather than re-deriving a parallel stringly-typed schema —
unlike aggregate events, an artifact's field set is already fixed by M2
and the ledger has no reason to treat it generically.

:func:`apply_artifact_metadata` records only already-published metadata:
it verifies the digest's bytes exist and are not corrupted in the given
:class:`~enginery.ledger.artifact_store.ArtifactStore` before writing
anything, matching "record only already-published artifact metadata" —
publishing bytes and recording their metadata are two separate steps, and
this function only ever performs the second one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import ArtifactMissingError


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


@dataclass(frozen=True, slots=True)
class ArtifactMetadataWrite:
    artifact_id: ArtifactId
    digest: Digest
    byte_size: int
    media_type: str
    kind: ArtifactKind
    run_id: RunId
    node_id: NodeId
    attempt_id: NodeAttemptId
    redaction: RedactionClassification
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.byte_size < 0:
            raise InvalidInputError(
                "byte_size cannot be negative", details={"byte_size": self.byte_size}
            )
        _require_non_blank(self.media_type, field_name="media_type")
        if self.schema_version < 1:
            raise InvalidInputError(
                "schema_version must be at least 1",
                details={"schema_version": self.schema_version},
            )


@dataclass(frozen=True, slots=True)
class ArtifactMetadataRecord:
    artifact_id: str
    digest: str
    byte_size: int
    media_type: str
    kind: str
    run_id: str
    node_id: str
    attempt_id: str
    storage_reference: str
    redaction: str
    created_at: str
    schema_version: int


def apply_artifact_metadata(
    connection: sqlite3.Connection,
    write: ArtifactMetadataWrite,
    *,
    store: ArtifactStore,
) -> None:
    """Insert one artifact's metadata row. Assumes a caller-owned
    transaction.

    Raises :class:`ArtifactMissingError` — folded into the caller's
    rollback — if ``store`` has no verifiably intact bytes for
    ``write.digest``.
    """
    if not store.verify(write.digest):
        raise ArtifactMissingError(
            f"cannot record metadata for unpublished or corrupted digest {write.digest}",
            details={"artifact_id": str(write.artifact_id), "digest": str(write.digest)},
        )
    storage_reference = str(store.path_for(write.digest))
    connection.execute(
        """
        INSERT INTO artifacts (
            artifact_id, digest, byte_size, media_type, kind, run_id, node_id,
            attempt_id, storage_reference, redaction, created_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(write.artifact_id),
            str(write.digest),
            write.byte_size,
            write.media_type,
            write.kind.value,
            str(write.run_id),
            str(write.node_id),
            str(write.attempt_id),
            storage_reference,
            write.redaction.value,
            datetime.now(UTC).isoformat(),
            write.schema_version,
        ),
    )


def read_artifact_metadata(
    connection: sqlite3.Connection, artifact_id: str
) -> ArtifactMetadataRecord | None:
    row = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:
        return None
    return ArtifactMetadataRecord(
        artifact_id=row["artifact_id"],
        digest=row["digest"],
        byte_size=row["byte_size"],
        media_type=row["media_type"],
        kind=row["kind"],
        run_id=row["run_id"],
        node_id=row["node_id"],
        attempt_id=row["attempt_id"],
        storage_reference=row["storage_reference"],
        redaction=row["redaction"],
        created_at=row["created_at"],
        schema_version=row["schema_version"],
    )


__all__ = [
    "ArtifactMetadataRecord",
    "ArtifactMetadataWrite",
    "apply_artifact_metadata",
    "read_artifact_metadata",
]
