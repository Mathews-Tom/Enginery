from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLeases
from enginery.ledger.service import LedgerService


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 18, 13, 0, tzinfo=UTC)


def test_result_ingestion_accepts_only_the_current_fenced_lease(
    ledger_service: LedgerService, now: datetime
) -> None:
    coordinator = Coordinator(ledger_service, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    leases = FencedNodeLeases(ledger_service, coordinator)
    lease = leases.grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=30),
        expected_attempt_version=0,
    )

    leases.ingest_result(
        lease=lease,
        now=now + timedelta(seconds=1),
        expected_attempt_version=1,
        result_state="passed",
        result_payload={"output_digest": "abc"},
    )

    with pytest.raises(ExternalConflictError):
        leases.ingest_result(
            lease=lease,
            now=now + timedelta(seconds=2),
            expected_attempt_version=2,
            result_state="passed",
            result_payload={},
        )


def test_stale_lease_token_cannot_commit_after_epoch_takeover(
    ledger_service: LedgerService, now: datetime
) -> None:
    first_coordinator = Coordinator(ledger_service, owner="coordinator-a")
    first_epoch = first_coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=10))
    first_leases = FencedNodeLeases(ledger_service, first_coordinator)
    stale_lease = first_leases.grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=first_epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=10),
        expected_attempt_version=0,
    )
    second_coordinator = Coordinator(ledger_service, owner="coordinator-b")
    takeover_time = now + timedelta(seconds=11)
    second_epoch = second_coordinator.acquire(
        now=takeover_time, heartbeat_window=timedelta(seconds=30)
    )
    second_leases = FencedNodeLeases(ledger_service, second_coordinator)
    current_lease = second_leases.grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-2",
        epoch=second_epoch.epoch,
        now=takeover_time,
        lease_window=timedelta(seconds=30),
        expected_attempt_version=0,
    )

    with pytest.raises(ExternalConflictError):
        first_leases.ingest_result(
            lease=stale_lease,
            now=takeover_time,
            expected_attempt_version=1,
            result_state="passed",
            result_payload={},
        )

    second_leases.ingest_result(
        lease=current_lease,
        now=takeover_time + timedelta(seconds=1),
        expected_attempt_version=1,
        result_state="passed",
        result_payload={},
    )
