"""Coordinator-owned fenced node leases and worker result ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.engine.coordinator import Coordinator
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.leases import LeaseRecord, LeaseWrite
from enginery.ledger.service import LedgerService


@dataclass(frozen=True, slots=True)
class FencedNodeLease:
    run_id: str
    node_id: str
    attempt_id: str
    epoch: int
    fencing_token: int
    owner: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.run_id, self.node_id, self.attempt_id, self.owner)
        ):
            raise InvalidInputError("fenced lease identifiers must be non-blank")
        if self.epoch < 1 or self.fencing_token < 1:
            raise InvalidInputError("fenced lease epoch and token must be positive")
        if self.expires_at.tzinfo is None:
            raise InvalidInputError("fenced lease expiry must be timezone-aware")


class FencedNodeLeases:
    """Grant and consume leases only under the coordinator's current epoch."""

    def __init__(self, ledger: LedgerService, coordinator: Coordinator) -> None:
        self._ledger = ledger
        self._coordinator = coordinator

    def grant(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt_id: str,
        epoch: int,
        now: datetime,
        lease_window: timedelta,
        expected_attempt_version: int,
    ) -> FencedNodeLease:
        """Atomically issue the next fencing token for an unleased node."""
        _require_aware(now, field_name="now")
        if lease_window <= timedelta():
            raise InvalidInputError("lease_window must be positive")
        if expected_attempt_version < 0:
            raise InvalidInputError("expected_attempt_version cannot be negative")
        current = self._ledger.read_lease(run_id=run_id, node_id=node_id)
        if current is not None and _lease_active(current, now=now):
            raise ExternalConflictError(
                "node already has an active lease",
                details={"run_id": run_id, "node_id": node_id},
            )
        token = 1 if current is None else current.fencing_token + 1
        expiry = now + lease_window
        lease = FencedNodeLease(
            run_id=run_id,
            node_id=node_id,
            attempt_id=attempt_id,
            epoch=epoch,
            fencing_token=token,
            owner=self._coordinator.owner,
            expires_at=expiry,
        )
        self._ledger.append(
            AppendCommand(
                correlation_id=f"node-lease-grant:{run_id}:{node_id}:{token}",
                events=(
                    EventWrite(
                        aggregate_type="node_attempt",
                        aggregate_id=attempt_id,
                        expected_version=expected_attempt_version,
                        event_type="node_attempt.leased",
                        schema_version=1,
                        payload=_lease_payload(lease),
                    ),
                    _lease_event(
                        self._ledger,
                        run_id=run_id,
                        node_id=node_id,
                        event_type="node_lease.granted",
                        payload=_lease_payload(lease),
                    ),
                ),
                process_manager_updates=(self._coordinator.epoch_guard(epoch=epoch, now=now),),
                lease_updates=(_lease_write(lease),),
            )
        )
        return lease

    def ingest_result(
        self,
        *,
        lease: FencedNodeLease,
        now: datetime,
        expected_attempt_version: int,
        result_state: str,
        result_payload: Mapping[str, object],
    ) -> None:
        """Accept one worker result only from the currently fenced lease."""
        _require_aware(now, field_name="now")
        if expected_attempt_version < 0:
            raise InvalidInputError("expected_attempt_version cannot be negative")
        if result_state not in {"passed", "failed", "cancelled"}:
            raise InvalidInputError("result_state must be passed, failed, or cancelled")
        current = self._ledger.read_lease(run_id=lease.run_id, node_id=lease.node_id)
        if current is None or not _matches(current, lease) or not _lease_active(current, now=now):
            raise ExternalConflictError(
                "worker result does not hold the current active node lease",
                details={
                    "run_id": lease.run_id,
                    "node_id": lease.node_id,
                    "epoch": lease.epoch,
                    "fencing_token": lease.fencing_token,
                },
            )
        payload = _lease_payload(lease)
        payload.update({"result_state": result_state, "result": dict(result_payload)})
        self._ledger.append(
            AppendCommand(
                correlation_id=f"node-result:{lease.run_id}:{lease.node_id}:{lease.fencing_token}",
                events=(
                    EventWrite(
                        aggregate_type="node_attempt",
                        aggregate_id=lease.attempt_id,
                        expected_version=expected_attempt_version,
                        event_type="node_attempt.result_ingested",
                        schema_version=1,
                        payload=payload,
                    ),
                    _lease_event(
                        self._ledger,
                        run_id=lease.run_id,
                        node_id=lease.node_id,
                        event_type="node_lease.result_ingested",
                        payload=payload,
                    ),
                ),
                process_manager_updates=(
                    self._coordinator.epoch_guard(epoch=lease.epoch, now=now),
                ),
                lease_updates=(
                    LeaseWrite(
                        run_id=lease.run_id,
                        node_id=lease.node_id,
                        epoch=lease.epoch,
                        fencing_token=lease.fencing_token,
                        owner=lease.owner,
                        attempt_id=lease.attempt_id,
                        expires_at=now.isoformat(),
                    ),
                ),
            )
        )


def _lease_event(
    ledger: LedgerService,
    *,
    run_id: str,
    node_id: str,
    event_type: str,
    payload: Mapping[str, object],
) -> EventWrite:
    aggregate_id = f"{run_id}:{node_id}"
    projection = ledger.read_projection(aggregate_type="node_lease", aggregate_id=aggregate_id)
    return EventWrite(
        aggregate_type="node_lease",
        aggregate_id=aggregate_id,
        expected_version=0 if projection is None else projection.aggregate_version,
        event_type=event_type,
        schema_version=1,
        payload=payload,
    )


def _lease_write(lease: FencedNodeLease) -> LeaseWrite:
    return LeaseWrite(
        run_id=lease.run_id,
        node_id=lease.node_id,
        epoch=lease.epoch,
        fencing_token=lease.fencing_token,
        owner=lease.owner,
        attempt_id=lease.attempt_id,
        expires_at=lease.expires_at.isoformat(),
    )


def _lease_payload(lease: FencedNodeLease) -> dict[str, object]:
    return {
        "run_id": lease.run_id,
        "node_id": lease.node_id,
        "attempt_id": lease.attempt_id,
        "epoch": lease.epoch,
        "fencing_token": lease.fencing_token,
        "owner": lease.owner,
        "expires_at": lease.expires_at.isoformat(),
    }


def _matches(record: LeaseRecord, lease: FencedNodeLease) -> bool:
    return (
        record.epoch == lease.epoch
        and record.fencing_token == lease.fencing_token
        and record.owner == lease.owner
        and record.attempt_id == lease.attempt_id
    )


def _lease_active(record: LeaseRecord, *, now: datetime) -> bool:
    if record.expires_at is None:
        raise InternalInvariantViolationError(
            "a node lease without an expiry cannot be automatically reused",
            details={"run_id": record.run_id, "node_id": record.node_id},
        )
    try:
        expires_at = datetime.fromisoformat(record.expires_at)
    except ValueError as error:
        raise InternalInvariantViolationError(
            "stored node lease expiry is invalid",
            details={"run_id": record.run_id, "node_id": record.node_id},
        ) from error
    _require_aware(expires_at, field_name="stored node lease expiry")
    return now < expires_at


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None:
        raise InvalidInputError(f"{field_name} must be timezone-aware")


__all__ = ["FencedNodeLease", "FencedNodeLeases"]
