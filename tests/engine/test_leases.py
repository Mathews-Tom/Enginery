from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.supervisor import ProcessIdentity, WorkerSupervisor
from enginery.ledger.service import LedgerService


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 18, 13, 0, tzinfo=UTC)


def _leased_worker(
    ledger: LedgerService, *, now: datetime, tmp_path: Path
) -> tuple[Coordinator, FencedNodeLease, ProcessIdentity]:
    coordinator = Coordinator(ledger, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    lease = FencedNodeLeases(ledger, coordinator).grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=30),
        expected_attempt_version=0,
        operation_id="operation-1",
    )
    identity = WorkerSupervisor(ledger, coordinator).start(
        lease=lease,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        cwd=tmp_path,
        now=now,
    )
    return coordinator, lease, identity


def _envelope(lease: FencedNodeLease) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        run_id=lease.run_id,
        node_id=lease.node_id,
        attempt_id=lease.attempt_id,
        epoch=lease.epoch,
        fencing_token=lease.fencing_token,
        operation_id=lease.operation_id,
        terminal_result="passed",
        artifact_references=("sha256:result",),
        result={"output_digest": "abc"},
    )


def test_result_ingestion_accepts_only_the_current_fenced_envelope(
    ledger_service: LedgerService, now: datetime, tmp_path: Path
) -> None:
    coordinator, lease, identity = _leased_worker(ledger_service, now=now, tmp_path=tmp_path)
    leases = FencedNodeLeases(ledger_service, coordinator)
    envelope = _envelope(lease)
    WorkerSupervisor(ledger_service, coordinator).cancel(
        lease=lease,
        identity=identity,
        now=now + timedelta(seconds=1),
    )

    leases.ingest_result(
        envelope=envelope,
        coordinator_epoch=lease.epoch,
        now=now + timedelta(seconds=1),
        expected_attempt_version=1,
    )

    with pytest.raises(ExternalConflictError, match="current active node lease"):
        leases.ingest_result(
            envelope=envelope,
            coordinator_epoch=lease.epoch,
            now=now + timedelta(seconds=2),
            expected_attempt_version=2,
        )


def test_result_rejects_mismatched_durable_operation(
    ledger_service: LedgerService, now: datetime, tmp_path: Path
) -> None:
    coordinator, lease, identity = _leased_worker(ledger_service, now=now, tmp_path=tmp_path)
    envelope = WorkerResultEnvelope(
        run_id=lease.run_id,
        node_id=lease.node_id,
        attempt_id=lease.attempt_id,
        epoch=lease.epoch,
        fencing_token=lease.fencing_token,
        operation_id="other-operation",
        terminal_result="passed",
        artifact_references=(),
        result={},
    )

    with pytest.raises(ExternalConflictError, match="operation does not match"):
        FencedNodeLeases(ledger_service, coordinator).ingest_result(
            envelope=envelope,
            coordinator_epoch=lease.epoch,
            now=now + timedelta(seconds=1),
            expected_attempt_version=1,
        )
    WorkerSupervisor(ledger_service, coordinator).cancel(
        lease=lease,
        identity=identity,
        now=now + timedelta(seconds=2),
    )


def test_expired_lease_cannot_be_reissued_without_reconciliation(
    ledger_service: LedgerService, now: datetime
) -> None:
    first = Coordinator(ledger_service, owner="coordinator-a")
    first_epoch = first.acquire(now=now, heartbeat_window=timedelta(seconds=10))
    FencedNodeLeases(ledger_service, first).grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=first_epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=10),
        expected_attempt_version=0,
        operation_id="operation-1",
    )
    replacement = Coordinator(ledger_service, owner="coordinator-b")
    replacement_epoch = replacement.acquire(
        now=now + timedelta(seconds=11), heartbeat_window=timedelta(seconds=30)
    )

    with pytest.raises(ExternalConflictError, match="requires prior process"):
        FencedNodeLeases(ledger_service, replacement).grant(
            run_id="run-1",
            node_id="node-1",
            attempt_id="attempt-2",
            epoch=replacement_epoch.epoch,
            now=now + timedelta(seconds=11),
            lease_window=timedelta(seconds=30),
            expected_attempt_version=0,
            operation_id="operation-2",
        )
