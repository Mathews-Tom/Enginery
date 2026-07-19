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
from enginery.engine.results import WorkerResultEnvelope
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
    operation_id: str
    owner: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.run_id, self.node_id, self.attempt_id, self.operation_id, self.owner)
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
        operation_id: str,
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
        if current is not None and not _prior_worker_reconciled(self._ledger, current):
            raise ExternalConflictError(
                "expired node lease requires prior process and workspace reconciliation",
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
            operation_id=operation_id,
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
        envelope: WorkerResultEnvelope,
        coordinator_epoch: int,
        now: datetime,
        expected_attempt_version: int,
        allow_expired_cancellation: bool = False,
    ) -> None:
        """Accept an exact current-lease worker envelope once.

        The process manager holds the operation ID because an operation is a
        worker-side idempotency boundary, not a lease-table concern.  Both the
        lease and supervisor record must match before an aggregate transition
        is allowed.
        """
        _require_aware(now, field_name="now")
        if expected_attempt_version < 0:
            raise InvalidInputError("expected_attempt_version cannot be negative")
        if allow_expired_cancellation and envelope.terminal_result != "cancelled":
            raise InvalidInputError("only a cancellation result may bypass lease liveness")
        current = self._ledger.read_lease(run_id=envelope.run_id, node_id=envelope.node_id)
        if (
            current is None
            or not _envelope_matches(current, envelope)
            or (not allow_expired_cancellation and not _lease_active(current, now=now))
        ):
            raise ExternalConflictError(
                "worker result does not hold the current active node lease",
                details={
                    "run_id": envelope.run_id,
                    "node_id": envelope.node_id,
                    "epoch": envelope.epoch,
                    "fencing_token": envelope.fencing_token,
                },
            )
        supervisor = self._ledger.read_process_manager_state(
            process_manager_name="worker-supervisor",
            state_key=f"{envelope.run_id}:{envelope.node_id}",
        )
        if (
            supervisor is None
            or supervisor.state.get("operation_id") != envelope.operation_id
            or supervisor.state.get("attempt_id") != envelope.attempt_id
        ):
            raise ExternalConflictError(
                "worker result operation does not match durable supervisor state",
                details={"run_id": envelope.run_id, "node_id": envelope.node_id},
            )
        if supervisor.state.get("status") not in {"exit_observed", "exit_reconciled"}:
            raise ExternalConflictError(
                "worker result arrived before supervised process exit was observed",
                details={"run_id": envelope.run_id, "node_id": envelope.node_id},
            )
        lease = FencedNodeLease(
            run_id=current.run_id,
            node_id=current.node_id,
            attempt_id=current.attempt_id,
            epoch=current.epoch,
            fencing_token=current.fencing_token,
            operation_id=envelope.operation_id,
            owner=current.owner,
            expires_at=_lease_expiry(current),
        )
        payload = _lease_payload(lease)
        payload.update(
            {
                "terminal_result": envelope.terminal_result,
                "artifact_references": list(envelope.artifact_references),
                "result": dict(envelope.result),
            }
        )
        self._ledger.append(
            AppendCommand(
                correlation_id=(
                    f"node-result:{lease.run_id}:{lease.node_id}:{lease.fencing_token}"
                ),
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
                    self._coordinator.epoch_guard(epoch=coordinator_epoch, now=now),
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
        "operation_id": lease.operation_id,
        "owner": lease.owner,
        "expires_at": lease.expires_at.isoformat(),
    }


def _envelope_matches(record: LeaseRecord, envelope: WorkerResultEnvelope) -> bool:
    return (
        record.epoch == envelope.epoch
        and record.fencing_token == envelope.fencing_token
        and record.attempt_id == envelope.attempt_id
    )


def _prior_worker_reconciled(ledger: LedgerService, record: LeaseRecord) -> bool:
    supervisor = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor",
        state_key=f"{record.run_id}:{record.node_id}",
    )
    return (
        supervisor is not None
        and supervisor.state.get("status") == "exit_reconciled"
        and supervisor.state.get("attempt_id") == record.attempt_id
        and supervisor.state.get("fencing_token") == record.fencing_token
    )


def _lease_expiry(record: LeaseRecord) -> datetime:
    if record.expires_at is None:
        raise InternalInvariantViolationError(
            "a node lease without an expiry cannot be used",
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
    return expires_at


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
