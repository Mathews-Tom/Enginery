from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from tests.workflows.test_stage1_runtime import _check_disposition_fixture

from enginery.domain.execution_selection import HoldReason
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1ProgressionAction, stage1_request_from_state


def test_historical_request_keeps_digest_and_omitted_checks(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, *_ = _check_disposition_fixture(ledger_service, tmp_path, start=False)
    assert request.behavioral_checks is None
    state = request.initial_state()
    assert "behavioral_checks" not in state
    decoded = stage1_request_from_state(state)
    assert decoded.digest == request.digest
    assert decoded.initial_state() == state


def test_new_empty_check_request_holds_without_rebinding(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    historical, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False
    )
    request = replace(historical, behavioral_checks=())
    assert request.digest != historical.digest
    assert stage1_request_from_state(request.initial_state()) == request
    service.start(request, now=now, heartbeat_window=timedelta(seconds=60))
    progression = service.next_action(request.run.id)
    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.NO_BEHAVIORAL_CHECKS
    assert "enginery stage1 build-request" in progression.hold.message
    assert service.read(request.run.id).request.digest == request.digest
