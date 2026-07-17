from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.inbox import acknowledge_command, enqueue_command
from enginery.ledger.service import LedgerService


def test_enqueue_command_is_pending_and_readable(ledger_service: LedgerService) -> None:
    record = ledger_service.enqueue_command(
        command_id="cmd-1",
        command_type="run.start",
        correlation_id="cmd-1",
        payload={"work_item_id": "wi-1"},
    )
    assert record.status == "pending"
    assert record.processed_at is None

    read = ledger_service.read_inbox_command("cmd-1")
    assert read is not None
    assert read.command_type == "run.start"


def test_read_unknown_command_returns_none(ledger_service: LedgerService) -> None:
    assert ledger_service.read_inbox_command("does-not-exist") is None


def test_enqueue_with_same_idempotency_key_returns_existing_record(
    ledger_service: LedgerService,
) -> None:
    first = ledger_service.enqueue_command(
        command_id="cmd-1",
        command_type="run.start",
        correlation_id="cmd-1",
        payload={"a": 1},
        idempotency_key="idem-1",
    )
    second = ledger_service.enqueue_command(
        command_id="cmd-2",  # a different attempted command_id
        command_type="run.start",
        correlation_id="cmd-2",
        payload={"a": 1},
        idempotency_key="idem-1",
    )
    assert second == first
    assert ledger_service.read_inbox_command("cmd-2") is None


def test_find_by_idempotency_key(ledger_service: LedgerService) -> None:
    ledger_service.enqueue_command(
        command_id="cmd-1",
        command_type="run.start",
        correlation_id="cmd-1",
        payload={},
        idempotency_key="idem-1",
    )
    found = ledger_service.find_inbox_command_by_idempotency_key("idem-1")
    assert found is not None
    assert found.command_id == "cmd-1"
    assert ledger_service.find_inbox_command_by_idempotency_key("missing") is None


def test_acknowledge_unknown_command_raises(ledger_service: LedgerService) -> None:
    with pytest.raises(ExpectedVersionConflictError):
        acknowledge_command(ledger_service.connection, "does-not-exist")


def test_acknowledge_already_processed_command_raises(ledger_service: LedgerService) -> None:
    enqueue_command(
        ledger_service.connection,
        command_id="cmd-1",
        command_type="run.start",
        correlation_id="cmd-1",
        payload={},
    )
    acknowledge_command(ledger_service.connection, "cmd-1")
    with pytest.raises(ExpectedVersionConflictError):
        acknowledge_command(ledger_service.connection, "cmd-1")


def test_enqueue_rejects_blank_command_id(ledger_service: LedgerService) -> None:
    with pytest.raises(InvalidInputError):
        ledger_service.enqueue_command(
            command_id="   ",
            command_type="run.start",
            correlation_id="cmd-1",
            payload={},
        )
