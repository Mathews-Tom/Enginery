from __future__ import annotations

import json

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.outbox import OutboxWrite
from enginery.ledger.service import LedgerService


def _event(**overrides: object) -> EventWrite:
    defaults: dict[str, object] = {
        "aggregate_type": "work_item",
        "aggregate_id": "wi-1",
        "expected_version": 0,
        "event_type": "work_item.created",
        "schema_version": 1,
        "payload": {"title": "example"},
    }
    defaults.update(overrides)
    return EventWrite(**defaults)  # type: ignore[arg-type]


def test_outbox_entry_is_written_with_the_command(ledger_service: LedgerService) -> None:
    result = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(),),
            outbox_entries=(OutboxWrite(target="work_ledger", payload={"issue": 42}),),
        )
    )
    assert len(result.outbox_ids) == 1

    pending = ledger_service.list_pending_outbox()
    assert len(pending) == 1
    assert pending[0].target == "work_ledger"
    assert pending[0].status == "pending"

    row = ledger_service.connection.execute(
        "SELECT payload FROM outbox WHERE outbox_id = ?", (pending[0].outbox_id,)
    ).fetchone()
    assert json.loads(row["payload"]) == {"issue": 42}


def test_outbox_entries_roll_back_with_a_failed_command(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="setup", events=(_event(),)))

    with pytest.raises(ExpectedVersionConflictError):
        ledger_service.append(
            AppendCommand(
                correlation_id="cmd-2",
                events=(_event(expected_version=0, event_type="work_item.qualified"),),
                outbox_entries=(OutboxWrite(target="work_ledger", payload={}),),
            )
        )

    assert ledger_service.list_pending_outbox() == ()


def test_mark_dispatched_removes_entry_from_pending(ledger_service: LedgerService) -> None:
    result = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(),),
            outbox_entries=(OutboxWrite(target="work_ledger", payload={}),),
        )
    )
    ledger_service.mark_outbox_dispatched(result.outbox_ids[0])
    assert ledger_service.list_pending_outbox() == ()

    row = ledger_service.connection.execute(
        "SELECT status, dispatched_at FROM outbox WHERE outbox_id = ?", (result.outbox_ids[0],)
    ).fetchone()
    assert row["status"] == "dispatched"
    assert row["dispatched_at"] is not None


def test_outbox_write_rejects_blank_target() -> None:
    with pytest.raises(InvalidInputError):
        OutboxWrite(target="  ", payload={})


def test_multiple_outbox_entries_share_the_command_correlation_id(
    ledger_service: LedgerService,
) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(),),
            outbox_entries=(
                OutboxWrite(target="work_ledger", payload={"a": 1}),
                OutboxWrite(target="scm", payload={"b": 2}),
            ),
        )
    )
    rows = ledger_service.connection.execute(
        "SELECT DISTINCT correlation_id FROM outbox"
    ).fetchall()
    assert [row["correlation_id"] for row in rows] == ["cmd-1"]
