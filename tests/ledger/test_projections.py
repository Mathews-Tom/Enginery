from __future__ import annotations

import dataclasses

import pytest

from enginery.ledger.errors import ExpectedVersionConflictError, SchemaVersionUnsupportedError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.projections import rebuild_projections
from enginery.ledger.service import LedgerService


def _event(**overrides: object) -> EventWrite:
    defaults: dict[str, object] = {
        "aggregate_type": "work_item",
        "aggregate_id": "wi-1",
        "expected_version": 0,
        "event_type": "work_item.created",
        "schema_version": 1,
        "payload": {"title": "first"},
    }
    defaults.update(overrides)
    return EventWrite(**defaults)  # type: ignore[arg-type]


def test_append_updates_the_projection_to_the_new_state(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    projection = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    assert projection is not None
    assert projection.aggregate_version == 1
    assert projection.state == {"title": "first"}
    assert projection.event_type == "work_item.created"


def test_projection_reflects_the_latest_transition_only(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-2",
            events=(
                _event(
                    expected_version=1,
                    event_type="work_item.qualified",
                    payload={"title": "first", "qualified": True},
                ),
            ),
        )
    )
    projection = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    assert projection is not None
    assert projection.aggregate_version == 2
    assert projection.event_type == "work_item.qualified"
    assert projection.state == {"title": "first", "qualified": True}


def test_read_missing_projection_returns_none(ledger_service: LedgerService) -> None:
    projection = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="missing")
    assert projection is None


def test_projection_is_not_written_when_the_command_fails(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    with pytest.raises(ExpectedVersionConflictError):
        ledger_service.append(
            AppendCommand(
                correlation_id="cmd-2",
                events=(_event(expected_version=0, event_type="work_item.qualified"),),
            )
        )
    projection = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    assert projection is not None
    assert projection.aggregate_version == 1  # unchanged by the failed command


def test_rebuild_reproduces_projections_from_events(ledger_service: LedgerService) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-2",
            events=(
                _event(aggregate_id="wi-2", payload={"title": "second"}),
                _event(
                    expected_version=1,
                    event_type="work_item.qualified",
                    payload={"title": "first", "qualified": True},
                ),
            ),
        )
    )

    before_wi1 = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    before_wi2 = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-2")

    report = ledger_service.rebuild_projections()
    assert report.aggregates_rebuilt == 2

    after_wi1 = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    after_wi2 = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-2")
    assert before_wi1 is not None
    assert before_wi2 is not None
    assert after_wi1 is not None
    assert after_wi2 is not None
    # updated_at legitimately changes on rebuild; every replayed field must not.
    assert dataclasses.replace(after_wi1, updated_at="") == dataclasses.replace(
        before_wi1, updated_at=""
    )
    assert dataclasses.replace(after_wi2, updated_at="") == dataclasses.replace(
        before_wi2, updated_at=""
    )


def test_rebuild_replaces_stale_projection_state(ledger_service: LedgerService) -> None:
    """A projection row hand-corrupted between rebuilds must be replaced,
    not merged with, the replayed state."""
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    ledger_service.connection.execute(
        "UPDATE projections SET state_json = '{\"corrupted\": true}' "
        "WHERE aggregate_type = 'work_item' AND aggregate_id = 'wi-1'"
    )

    rebuild_projections(ledger_service.connection)
    projection = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    assert projection is not None
    assert projection.state == {"title": "first"}


def test_rebuild_stops_on_unsupported_schema_version_and_preserves_prior_state(
    ledger_service: LedgerService,
) -> None:
    ledger_service.append(AppendCommand(correlation_id="cmd-1", events=(_event(),)))
    before = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")

    ledger_service.connection.execute(
        "UPDATE events SET schema_version = 99 "
        "WHERE aggregate_type = 'work_item' AND aggregate_id = 'wi-1'"
    )

    with pytest.raises(SchemaVersionUnsupportedError):
        rebuild_projections(ledger_service.connection, max_supported_schema_version=1)

    after = ledger_service.read_projection(aggregate_type="work_item", aggregate_id="wi-1")
    assert after == before


def test_rebuild_on_an_empty_ledger_produces_no_projections(ledger_service: LedgerService) -> None:
    report = ledger_service.rebuild_projections()
    assert report.aggregates_rebuilt == 0
