from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def test_only_one_active_epoch_can_own_the_ledger(
    ledger_service: LedgerService, now: datetime
) -> None:
    first = Coordinator(ledger_service, owner="coordinator-a")
    second = Coordinator(ledger_service, owner="coordinator-b")

    epoch_one = first.acquire(now=now, heartbeat_window=timedelta(seconds=30))

    with pytest.raises(ExternalConflictError):
        second.acquire(now=now, heartbeat_window=timedelta(seconds=30))

    epoch_two = second.acquire(
        now=now + timedelta(seconds=31), heartbeat_window=timedelta(seconds=30)
    )

    assert epoch_one.epoch == 1
    assert epoch_two.epoch == 2
    with pytest.raises(ExternalConflictError):
        first.renew(
            epoch=epoch_one.epoch,
            now=now + timedelta(seconds=31),
            heartbeat_window=timedelta(seconds=30),
        )


def test_active_epoch_consumes_cancel_command_atomically(
    ledger_service: LedgerService, now: datetime
) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="run-created",
            events=(
                EventWrite(
                    aggregate_type="run",
                    aggregate_id="run-1",
                    expected_version=0,
                    event_type="run.created",
                    schema_version=1,
                    payload={},
                ),
            ),
        )
    )
    ledger_service.enqueue_command(
        command_id="cancel-1",
        command_type="run.cancel",
        correlation_id="cancel-1",
        payload={"run_id": "run-1", "expected_run_version": 1},
    )
    coordinator = Coordinator(ledger_service, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=30))

    consumed = coordinator.consume_pending(
        epoch=epoch.epoch,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=30),
    )

    assert consumed[0].status == "processed"
    command = ledger_service.read_inbox_command("cancel-1")
    assert command is not None
    assert command.status == "processed"
    run = ledger_service.read_projection(aggregate_type="run", aggregate_id="run-1")
    assert run is not None
    assert run.event_type == "run.cancellation_requested"


def test_stale_epoch_cannot_consume_a_pending_command(
    ledger_service: LedgerService, now: datetime
) -> None:
    ledger_service.enqueue_command(
        command_id="invalid-1",
        command_type="unrecognized.command",
        correlation_id="invalid-1",
        payload={},
    )
    first = Coordinator(ledger_service, owner="coordinator-a")
    second = Coordinator(ledger_service, owner="coordinator-b")
    first_epoch = first.acquire(now=now, heartbeat_window=timedelta(seconds=30))
    second_epoch = second.acquire(
        now=now + timedelta(seconds=31), heartbeat_window=timedelta(seconds=30)
    )

    with pytest.raises(ExternalConflictError):
        first.consume_pending(
            epoch=first_epoch.epoch,
            now=now + timedelta(seconds=31),
            heartbeat_window=timedelta(seconds=30),
        )

    pending = ledger_service.read_inbox_command("invalid-1")
    assert pending is not None
    assert pending.status == "pending"
    consumed = second.consume_pending(
        epoch=second_epoch.epoch,
        now=now + timedelta(seconds=32),
        heartbeat_window=timedelta(seconds=30),
    )
    assert consumed == (type(consumed[0])("invalid-1", "rejected"),)
    rejected = ledger_service.read_inbox_command("invalid-1")
    assert rejected is not None
    assert rejected.status == "rejected"
