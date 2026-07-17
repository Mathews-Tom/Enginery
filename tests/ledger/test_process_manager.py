from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
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


def test_process_manager_update_creates_state_at_version_one(
    ledger_service: LedgerService,
) -> None:
    result = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(),),
            process_manager_updates=(
                ProcessManagerStateWrite(
                    process_manager_name="plan_scheduler",
                    state_key="run-1",
                    expected_version=0,
                    state={"pending_nodes": ["a", "b"]},
                ),
            ),
        )
    )
    assert result.process_manager_states[0].state_version == 1

    state = ledger_service.read_process_manager_state(
        process_manager_name="plan_scheduler", state_key="run-1"
    )
    assert state is not None
    assert state.state == {"pending_nodes": ["a", "b"]}


def test_process_manager_update_with_stale_version_rolls_back_the_whole_command(
    ledger_service: LedgerService,
) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="setup",
            events=(_event(),),
            process_manager_updates=(
                ProcessManagerStateWrite(
                    process_manager_name="plan_scheduler",
                    state_key="run-1",
                    expected_version=0,
                    state={"pending_nodes": ["a"]},
                ),
            ),
        )
    )

    with pytest.raises(ExpectedVersionConflictError):
        ledger_service.append(
            AppendCommand(
                correlation_id="cmd-2",
                events=(_event(expected_version=1, event_type="work_item.qualified"),),
                process_manager_updates=(
                    ProcessManagerStateWrite(
                        process_manager_name="plan_scheduler",
                        state_key="run-1",
                        expected_version=0,  # stale: already at version 1
                        state={"pending_nodes": []},
                    ),
                ),
            )
        )

    # the event in the failed command must not have landed either
    row = ledger_service.connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = 'work_item' "
        "AND aggregate_id = 'wi-1'"
    ).fetchone()
    assert row["version"] == 1

    state = ledger_service.read_process_manager_state(
        process_manager_name="plan_scheduler", state_key="run-1"
    )
    assert state is not None
    assert state.state_version == 1
    assert state.state == {"pending_nodes": ["a"]}


def test_read_missing_process_manager_state_returns_none(ledger_service: LedgerService) -> None:
    assert (
        ledger_service.read_process_manager_state(
            process_manager_name="plan_scheduler", state_key="missing"
        )
        is None
    )


def test_process_manager_write_rejects_negative_expected_version() -> None:
    with pytest.raises(InvalidInputError):
        ProcessManagerStateWrite(
            process_manager_name="plan_scheduler",
            state_key="run-1",
            expected_version=-1,
            state={},
        )
