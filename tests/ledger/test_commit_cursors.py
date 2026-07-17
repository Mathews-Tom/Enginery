from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService


def _event(**overrides: object) -> EventWrite:
    defaults: dict[str, object] = {
        "aggregate_type": "work_item",
        "aggregate_id": "wi-1",
        "expected_version": 0,
        "event_type": "work_item.created",
        "schema_version": 1,
        "payload": {},
    }
    defaults.update(overrides)
    return EventWrite(**defaults)  # type: ignore[arg-type]


def test_new_consumer_cursor_starts_at_zero(ledger_service: LedgerService) -> None:
    assert ledger_service.read_cursor("jsonl-watch") == 0


def test_advance_cursor_persists_and_is_readable(ledger_service: LedgerService) -> None:
    result = ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    commit_seq = result.events[0].commit_seq

    ledger_service.advance_cursor("jsonl-watch", commit_seq)
    assert ledger_service.read_cursor("jsonl-watch") == commit_seq


def test_advance_cursor_rejects_regression_without_force(ledger_service: LedgerService) -> None:
    ledger_service.advance_cursor("jsonl-watch", 5)
    with pytest.raises(InvalidInputError):
        ledger_service.advance_cursor("jsonl-watch", 2)
    assert ledger_service.read_cursor("jsonl-watch") == 5


def test_advance_cursor_allows_forced_regression(ledger_service: LedgerService) -> None:
    ledger_service.advance_cursor("jsonl-watch", 5)
    ledger_service.advance_cursor("jsonl-watch", 2, force=True)
    assert ledger_service.read_cursor("jsonl-watch") == 2


def test_advance_cursor_rejects_negative_value(ledger_service: LedgerService) -> None:
    with pytest.raises(InvalidInputError):
        ledger_service.advance_cursor("jsonl-watch", -1)


def test_cursors_are_independent_per_consumer(ledger_service: LedgerService) -> None:
    ledger_service.advance_cursor("jsonl-watch", 3)
    ledger_service.advance_cursor("outbox-dispatcher", 7)
    assert ledger_service.read_cursor("jsonl-watch") == 3
    assert ledger_service.read_cursor("outbox-dispatcher") == 7


def test_commit_seq_survives_replay_and_matches_cursor_semantics(
    ledger_service: LedgerService,
) -> None:
    """After replay (projection rebuild), commit_seq values already
    advanced to by a cursor remain a valid resume point — replay does not
    renumber or invalidate the global commit sequence."""
    first = ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    second = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-2",
            events=(_event(expected_version=1, event_type="work_item.qualified"),),
        )
    )
    ledger_service.advance_cursor("jsonl-watch", first.events[0].commit_seq)
    ledger_service.rebuild_projections()

    assert ledger_service.read_cursor("jsonl-watch") == first.events[0].commit_seq
    assert second.events[0].commit_seq > first.events[0].commit_seq
