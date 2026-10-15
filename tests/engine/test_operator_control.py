from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import HumanActionRequiredError, InvalidInputError
from enginery.engine.coordinator import Coordinator
from enginery.engine.operator_control import (
    PAUSED_MESSAGE,
    ControlState,
    OperatorControl,
    SelectedRunner,
    list_workers,
    read_control_state,
    require_dispatch_allowed,
)
from enginery.engine.runtime import FixtureDispatch, FixtureRuntime
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.service import LedgerService

_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _runner(image: str = "sha256:" + "cd" * 32) -> SelectedRunner:
    return SelectedRunner(
        image=image,
        docker_context=None,
        profile="enginery-isolated-oci-v1",
        profile_digest="sha256:" + "ef" * 32,
        engine={"kind": "docker"},
        image_identity={"image_id": image},
        selected_at=_NOW.isoformat(),
    )


def test_default_state_is_running_with_no_runner(ledger_service: LedgerService) -> None:
    state = read_control_state(ledger_service)

    assert state.paused is False and state.selected_runner is None and state.state_version == 0
    require_dispatch_allowed(ledger_service)
    assert list_workers(ledger_service) == ()


def test_pause_resume_select_share_one_atomic_versioned_record(
    ledger_service: LedgerService,
) -> None:
    control = OperatorControl(ledger_service)

    control.select_runner(_runner(), now=_NOW)
    paused = control.pause(now=_NOW + timedelta(seconds=1), reason="window")
    resumed = control.resume(now=_NOW + timedelta(seconds=2))

    assert paused.state.selected_runner is not None  # a pause keeps the selected runner
    assert resumed.state.paused is False and resumed.state.selected_runner is not None
    assert resumed.state.state_version == 3
    reread = read_control_state(ledger_service)
    assert reread == resumed.state
    projection = ledger_service.read_projection(
        aggregate_type="operator_control", aggregate_id="control"
    )
    assert projection is not None and projection.aggregate_version == 3


def test_paused_state_refuses_dispatch_with_the_operator_message(
    ledger_service: LedgerService,
) -> None:
    OperatorControl(ledger_service).pause(now=_NOW)

    with pytest.raises(HumanActionRequiredError, match=PAUSED_MESSAGE):
        require_dispatch_allowed(ledger_service)


def test_fixture_runtime_dispatch_refuses_while_paused_before_any_effect(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    OperatorControl(ledger_service).pause(now=_NOW)
    runtime = FixtureRuntime(ledger_service, Coordinator(ledger_service, owner="coordinator"))
    request = FixtureDispatch(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        repository_id="repository-1",
        repository_path=tmp_path / "repository",
        workspace_path=tmp_path / "workspace",
        base_revision="0" * 40,
        command=(sys.executable, "-c", "pass"),
        expected_attempt_version=0,
        operation_id="operation-1",
    )

    with pytest.raises(HumanActionRequiredError, match=PAUSED_MESSAGE):
        runtime.dispatch(request=request, epoch=1, now=_NOW, lease_window=timedelta(seconds=30))

    assert not (tmp_path / "workspace").exists()
    assert (
        ledger_service.list_process_manager_states(process_manager_name="worker-supervisor") == ()
    )
    assert (
        ledger_service.list_process_manager_states(process_manager_name="workspace-reservations")
        == ()
    )


def test_a_stale_writer_cannot_overwrite_a_concurrent_control_change(
    ledger_path: Path, ledger_service: LedgerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = LedgerService.open(ledger_path)
    try:
        OperatorControl(other).pause(now=_NOW)
        # A writer that read the control state before the pause above must conflict, not clobber.
        monkeypatch.setattr(OperatorControl, "state", lambda self: ControlState())
        with pytest.raises(ExpectedVersionConflictError):
            OperatorControl(ledger_service).pause(now=_NOW)
    finally:
        other.close()
    monkeypatch.undo()
    assert read_control_state(ledger_service).paused is True


def test_control_time_must_be_timezone_aware(ledger_service: LedgerService) -> None:
    with pytest.raises(InvalidInputError):
        OperatorControl(ledger_service).pause(now=datetime(2026, 10, 13, 12, 0))
