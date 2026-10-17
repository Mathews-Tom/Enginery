from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from tests.workflows.test_stage1_runtime import _check_disposition_fixture

from enginery.domain.errors import HumanActionRequiredError, InvalidInputError
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import stage1_request_from_state


def test_historical_request_keeps_digest_and_omitted_checks(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, *_ = _check_disposition_fixture(ledger_service, tmp_path, start=False)
    request = replace(request, behavioral_checks=None)
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
    with pytest.raises(HumanActionRequiredError, match="enginery stage1 build-request"):
        service.start(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert (
        ledger_service.read_projection(aggregate_type="run", aggregate_id=str(request.run.id))
        is None
    )


def test_absent_checks_refused_for_new_run_but_history_restarts(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False
    )
    request = replace(request, behavioral_checks=None)
    with pytest.raises(InvalidInputError, match="new runs require bound behavioral checks"):
        service.start(request, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.register_run(
        run_id=str(request.run.id),
        initial_state=request.initial_state(),
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )
    assert (
        service.start(request, now=now, heartbeat_window=timedelta(seconds=60)).request == request
    )
