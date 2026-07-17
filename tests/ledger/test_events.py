from __future__ import annotations

import json

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite, append
from enginery.ledger.service import LedgerService


def _event(
    *,
    aggregate_type: str = "work_item",
    aggregate_id: str = "wi-1",
    expected_version: int = 0,
    event_type: str = "work_item.created",
    payload: dict[str, object] | None = None,
    causation_id: str | None = None,
) -> EventWrite:
    return EventWrite(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        expected_version=expected_version,
        event_type=event_type,
        schema_version=1,
        payload=payload if payload is not None else {"title": "example"},
        causation_id=causation_id,
    )


def test_append_creates_a_new_aggregate_at_version_one(ledger_service: LedgerService) -> None:
    result = ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))

    assert len(result.events) == 1
    appended = result.events[0]
    assert appended.aggregate_version == 1
    assert appended.commit_seq >= 1

    row = ledger_service.connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = ? AND aggregate_id = ?",
        ("work_item", "wi-1"),
    ).fetchone()
    assert row["version"] == 1


def test_append_second_event_with_correct_expected_version_advances_it(
    ledger_service: LedgerService,
) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    result = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-2",
            events=(_event(expected_version=1, event_type="work_item.qualified"),),
        )
    )

    assert result.events[0].aggregate_version == 2


def test_stale_expected_version_raises_and_writes_nothing(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))

    with pytest.raises(ExpectedVersionConflictError):
        ledger_service.append(
            AppendCommand(
                correlation_id="cmd-2",
                events=(_event(expected_version=0, event_type="work_item.qualified"),),
            )
        )

    row = ledger_service.connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = ? AND aggregate_id = ?",
        ("work_item", "wi-1"),
    ).fetchone()
    assert row["version"] == 1
    event_count = ledger_service.connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    assert event_count["n"] == 1


def test_multi_aggregate_command_commits_all_events_or_none(
    ledger_service: LedgerService,
) -> None:
    """One command spanning two aggregates: a version conflict on the
    second aggregate must undo the first aggregate's already-written event
    from the same command."""
    ledger_service.append(
        AppendCommand(
            correlation_id="setup",
            events=(_event(aggregate_type="run", aggregate_id="run-1"),),
        )
    )

    with pytest.raises(ExpectedVersionConflictError):
        ledger_service.append(
            AppendCommand(
                correlation_id="cmd-multi",
                events=(
                    _event(aggregate_type="work_item", aggregate_id="wi-multi"),
                    _event(
                        aggregate_type="run",
                        aggregate_id="run-1",
                        expected_version=0,  # stale: run-1 is already at version 1
                        event_type="run.queued",
                    ),
                ),
            )
        )

    work_item_row = ledger_service.connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = ? AND aggregate_id = ?",
        ("work_item", "wi-multi"),
    ).fetchone()
    assert work_item_row is None

    run_row = ledger_service.connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = ? AND aggregate_id = ?",
        ("run", "run-1"),
    ).fetchone()
    assert run_row["version"] == 1

    event_count = ledger_service.connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    assert event_count["n"] == 1


def test_multi_aggregate_command_commits_together_when_valid(
    ledger_service: LedgerService,
) -> None:
    result = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-multi",
            events=(
                _event(aggregate_type="work_item", aggregate_id="wi-a"),
                _event(aggregate_type="run", aggregate_id="run-a"),
            ),
        )
    )
    assert {e.aggregate_id for e in result.events} == {"wi-a", "run-a"}
    event_count = ledger_service.connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    assert event_count["n"] == 2


def test_payload_round_trips_as_canonical_json(ledger_service: LedgerService) -> None:
    payload = {"b": 2, "a": 1, "nested": {"z": True}}
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(payload=payload),)))

    row = ledger_service.connection.execute("SELECT payload FROM events").fetchone()
    assert json.loads(row["payload"]) == payload


def test_causation_id_defaults_to_correlation_id(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    row = ledger_service.connection.execute(
        "SELECT correlation_id, causation_id FROM events"
    ).fetchone()
    assert row["correlation_id"] == "cmd-1"
    assert row["causation_id"] == "cmd-1"


def test_causation_id_can_be_set_explicitly(ledger_service: LedgerService) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(causation_id="policy-decision-9"),),
        )
    )
    row = ledger_service.connection.execute("SELECT causation_id FROM events").fetchone()
    assert row["causation_id"] == "policy-decision-9"


def test_commit_seq_is_globally_monotonic_across_commands(ledger_service: LedgerService) -> None:
    first = ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    second = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-2",
            events=(_event(aggregate_id="wi-2"),),
        )
    )
    assert second.events[0].commit_seq > first.events[0].commit_seq


def test_empty_events_tuple_is_rejected() -> None:
    with pytest.raises(InvalidInputError):
        AppendCommand(correlation_id="cmd-1", events=())


def test_blank_aggregate_type_is_rejected() -> None:
    with pytest.raises(InvalidInputError):
        _event(aggregate_type="   ")


def test_negative_expected_version_is_rejected() -> None:
    with pytest.raises(InvalidInputError):
        _event(expected_version=-1)


def test_append_function_matches_service_append(ledger_service: LedgerService) -> None:
    command = AppendCommand(correlation_id="cmd-1", events=(_event(),))
    direct_result = append(ledger_service.connection, command)
    assert direct_result.events[0].aggregate_version == 1
