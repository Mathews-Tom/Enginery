from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.leases import LeaseWrite
from enginery.ledger.service import LedgerService


def _event(**overrides: object) -> EventWrite:
    defaults: dict[str, object] = {
        "aggregate_type": "run",
        "aggregate_id": "run-1",
        "expected_version": 0,
        "event_type": "run.created",
        "schema_version": 1,
        "payload": {},
    }
    defaults.update(overrides)
    return EventWrite(**defaults)  # type: ignore[arg-type]


def test_lease_update_is_written_and_readable(ledger_service: LedgerService) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(),),
            lease_updates=(
                LeaseWrite(
                    run_id="run-1",
                    node_id="node-a",
                    epoch=1,
                    fencing_token=1,
                    owner="coordinator-1",
                ),
            ),
        )
    )
    lease = ledger_service.read_lease(run_id="run-1", node_id="node-a")
    assert lease is not None
    assert lease.epoch == 1
    assert lease.fencing_token == 1
    assert lease.owner == "coordinator-1"


def test_lease_update_overwrites_the_prior_holder(ledger_service: LedgerService) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_event(),),
            lease_updates=(
                LeaseWrite(
                    run_id="run-1", node_id="node-a", epoch=1, fencing_token=1, owner="worker-1"
                ),
            ),
        )
    )
    ledger_service.append(
        AppendCommand(
            correlation_id="cmd-2",
            events=(_event(expected_version=1, event_type="run.advanced"),),
            lease_updates=(
                LeaseWrite(
                    run_id="run-1", node_id="node-a", epoch=2, fencing_token=2, owner="worker-2"
                ),
            ),
        )
    )
    lease = ledger_service.read_lease(run_id="run-1", node_id="node-a")
    assert lease is not None
    assert lease.epoch == 2
    assert lease.fencing_token == 2
    assert lease.owner == "worker-2"


def test_read_missing_lease_returns_none(ledger_service: LedgerService) -> None:
    assert ledger_service.read_lease(run_id="run-x", node_id="node-x") is None


def test_lease_write_rejects_blank_owner() -> None:
    with pytest.raises(InvalidInputError):
        LeaseWrite(run_id="run-1", node_id="node-a", epoch=1, fencing_token=1, owner="  ")


def test_lease_write_rejects_negative_fencing_token() -> None:
    with pytest.raises(InvalidInputError):
        LeaseWrite(run_id="run-1", node_id="node-a", epoch=1, fencing_token=-1, owner="worker-1")


def test_stale_lease_update_rolls_back_the_entire_command(ledger_service: LedgerService) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="initial",
            events=(_event(),),
            lease_updates=(
                LeaseWrite(
                    run_id="run-1",
                    node_id="node-a",
                    epoch=2,
                    fencing_token=2,
                    owner="worker-2",
                ),
            ),
        )
    )

    with pytest.raises(ExpectedVersionConflictError, match="current fencing token"):
        ledger_service.append(
            AppendCommand(
                correlation_id="stale",
                events=(_event(expected_version=1, event_type="run.stale"),),
                lease_updates=(
                    LeaseWrite(
                        run_id="run-1",
                        node_id="node-a",
                        epoch=1,
                        fencing_token=1,
                        owner="worker-1",
                    ),
                ),
            )
        )

    run = ledger_service.read_projection(aggregate_type="run", aggregate_id="run-1")
    assert run is not None
    assert run.aggregate_version == 1
