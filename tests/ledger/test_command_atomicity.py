"""End-to-end coverage of the one-transaction-per-command contract: a
single ``AppendCommand`` can carry events, an inbox acknowledgement,
outbox entries, process-manager updates, and lease updates, and every
piece commits together or none of them do.
"""

from __future__ import annotations

import pytest

from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.leases import LeaseWrite
from enginery.ledger.outbox import OutboxWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.service import LedgerService


def test_full_command_commits_every_piece_together(ledger_service: LedgerService) -> None:
    ledger_service.enqueue_command(
        command_id="cmd-1",
        command_type="run.start",
        correlation_id="cmd-1",
        payload={"work_item_id": "wi-1"},
    )

    result = ledger_service.append(
        AppendCommand(
            correlation_id="cmd-1",
            inbox_command_id="cmd-1",
            events=(
                EventWrite(
                    aggregate_type="run",
                    aggregate_id="run-1",
                    expected_version=0,
                    event_type="run.created",
                    schema_version=1,
                    payload={"work_item_id": "wi-1"},
                ),
            ),
            outbox_entries=(OutboxWrite(target="work_ledger", payload={"run_id": "run-1"}),),
            process_manager_updates=(
                ProcessManagerStateWrite(
                    process_manager_name="run_coordinator",
                    state_key="run-1",
                    expected_version=0,
                    state={"phase": "preflight"},
                ),
            ),
            lease_updates=(
                LeaseWrite(
                    run_id="run-1",
                    node_id="normalize",
                    epoch=1,
                    fencing_token=1,
                    owner="coordinator-1",
                ),
            ),
        )
    )

    assert result.inbox_acknowledged is True
    assert len(result.outbox_ids) == 1
    assert result.process_manager_states[0].state_version == 1

    assert ledger_service.read_inbox_command("cmd-1").status == "processed"  # type: ignore[union-attr]
    assert len(ledger_service.list_pending_outbox()) == 1
    assert (
        ledger_service.read_process_manager_state(
            process_manager_name="run_coordinator", state_key="run-1"
        )
        is not None
    )
    assert ledger_service.read_lease(run_id="run-1", node_id="normalize") is not None


def test_process_manager_conflict_rolls_back_inbox_ack_outbox_and_lease(
    ledger_service: LedgerService,
) -> None:
    """A stale process-manager expected_version must undo every other
    write attempted in the same command, including the inbox
    acknowledgement, outbox entries, and lease updates that would
    otherwise have committed."""
    ledger_service.enqueue_command(
        command_id="cmd-1",
        command_type="run.start",
        correlation_id="cmd-1",
        payload={},
    )
    ledger_service.append(
        AppendCommand(
            correlation_id="setup",
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
            process_manager_updates=(
                ProcessManagerStateWrite(
                    process_manager_name="run_coordinator",
                    state_key="run-1",
                    expected_version=0,
                    state={"phase": "preflight"},
                ),
            ),
        )
    )

    with pytest.raises(ExpectedVersionConflictError):
        ledger_service.append(
            AppendCommand(
                correlation_id="cmd-1",
                inbox_command_id="cmd-1",
                events=(
                    EventWrite(
                        aggregate_type="run",
                        aggregate_id="run-1",
                        expected_version=1,
                        event_type="run.queued",
                        schema_version=1,
                        payload={},
                    ),
                ),
                outbox_entries=(OutboxWrite(target="work_ledger", payload={}),),
                process_manager_updates=(
                    ProcessManagerStateWrite(
                        process_manager_name="run_coordinator",
                        state_key="run-1",
                        expected_version=0,  # stale: already at version 1
                        state={"phase": "queued"},
                    ),
                ),
                lease_updates=(
                    LeaseWrite(
                        run_id="run-1",
                        node_id="normalize",
                        epoch=1,
                        fencing_token=1,
                        owner="coordinator-1",
                    ),
                ),
            )
        )

    inbox_record = ledger_service.read_inbox_command("cmd-1")
    assert inbox_record is not None
    assert inbox_record.status == "pending"
    assert ledger_service.list_pending_outbox() == ()
    assert ledger_service.read_lease(run_id="run-1", node_id="normalize") is None
    run_row = ledger_service.connection.execute(
        "SELECT version FROM aggregates WHERE aggregate_type = 'run' AND aggregate_id = 'run-1'"
    ).fetchone()
    assert run_row["version"] == 1
