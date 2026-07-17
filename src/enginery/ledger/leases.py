"""Node leases: durable epoch and fencing-token state per (run, node).

Schema and write path only — the coordinator that grants, renews, and
fences leases is M5. Recording ``epoch`` and ``fencing_token`` here now
means the eventual scheduler only has to persist through this API, not
design its own lease table under time pressure.

Lease writes are idempotent upserts keyed by ``(run_id, node_id)``: this
module does not enforce fencing-token monotonicity itself, because the
holder-vs-fencing comparison is the coordinator's business rule (M5), not
a ledger-storage invariant. The ledger's job is durability, not policy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


@dataclass(frozen=True, slots=True)
class LeaseWrite:
    run_id: str
    node_id: str
    epoch: int
    fencing_token: int
    owner: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _require_non_blank(self.run_id, field_name="run_id")
        _require_non_blank(self.node_id, field_name="node_id")
        _require_non_blank(self.owner, field_name="owner")
        if self.epoch < 0:
            raise InvalidInputError("epoch cannot be negative", details={"epoch": self.epoch})
        if self.fencing_token < 0:
            raise InvalidInputError(
                "fencing_token cannot be negative",
                details={"fencing_token": self.fencing_token},
            )


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    run_id: str
    node_id: str
    epoch: int
    fencing_token: int
    owner: str
    granted_at: str
    expires_at: str | None


def apply_lease_update(connection: sqlite3.Connection, write: LeaseWrite) -> None:
    """Upsert one node lease. Assumes a caller-owned transaction."""
    connection.execute(
        """
        INSERT INTO node_leases (
            run_id, node_id, epoch, fencing_token, owner, granted_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, node_id)
        DO UPDATE SET epoch = excluded.epoch,
                       fencing_token = excluded.fencing_token,
                       owner = excluded.owner,
                       granted_at = excluded.granted_at,
                       expires_at = excluded.expires_at
        """,
        (
            write.run_id,
            write.node_id,
            write.epoch,
            write.fencing_token,
            write.owner,
            datetime.now(UTC).isoformat(),
            write.expires_at,
        ),
    )


def read_lease(connection: sqlite3.Connection, *, run_id: str, node_id: str) -> LeaseRecord | None:
    row = connection.execute(
        "SELECT * FROM node_leases WHERE run_id = ? AND node_id = ?", (run_id, node_id)
    ).fetchone()
    if row is None:
        return None
    return LeaseRecord(
        run_id=row["run_id"],
        node_id=row["node_id"],
        epoch=row["epoch"],
        fencing_token=row["fencing_token"],
        owner=row["owner"],
        granted_at=row["granted_at"],
        expires_at=row["expires_at"],
    )


__all__ = ["LeaseRecord", "LeaseWrite", "apply_lease_update", "read_lease"]
