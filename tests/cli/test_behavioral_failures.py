from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.cli import factory_behavioral
from enginery.cli.config_binding import bind_behavioral_checks
from enginery.cli.config_locations import private_state_directory
from enginery.domain.errors import HumanActionRequiredError
from enginery.domain.node_attempt import EvidenceResult
from enginery.ledger.service import LedgerService
from tests.cli.test_engine_runs import _request


def test_stage1_verifier_creates_missing_calibration_without_sidecar(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        workspace_path=request.repository_path,
        execution_configuration=replace(
            request.execution_configuration,
            artifact_root=private_state_directory(request.repository_path) / "artifacts",
        ),
    )
    bind_behavioral_checks(request)
    with LedgerService.open(tmp_path / "manual ledger.sqlite") as ledger:
        digest = factory_behavioral.verify_for_stage1(
            ledger, request, request.run.base_revision, owner="manual owner"
        )
        verdict = ledger.read_projection(
            aggregate_type="behavioral_acceptance", aggregate_id=str(request.run.id)
        )
        assert verdict is not None
        assert verdict.state["result"] == EvidenceResult.PASS.value
        assert verdict.state["receipt_digest"] == str(digest)


@pytest.mark.parametrize("result", [EvidenceResult.FAIL, EvidenceResult.INDETERMINATE])
def test_stage1_failure_repairs_retry_its_actual_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: EvidenceResult
) -> None:
    request = _request(tmp_path)
    calls: list[bool] = []

    def recorded(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs.get("calibrate_when_required") is True)
        return {"result": result.value, "holds": ["behavioral_assertion_failed:fixture:0"]}

    monkeypatch.setattr(factory_behavioral, "execute_behavioral", recorded)
    database = tmp_path / "manual ledger.sqlite"
    with (
        LedgerService.open(database) as ledger,
        pytest.raises(HumanActionRequiredError) as caught,
    ):
        factory_behavioral.verify_for_stage1(
            ledger, request, request.run.base_revision, owner="owner's terminal"
        )
    assert calls == [True]
    reason = (
        "behavioral_acceptance_failed"
        if result is EvidenceResult.FAIL
        else "behavioral_acceptance_indeterminate"
    )
    assert caught.value.details["reason"] == reason
    assert "behavioral_assertion_failed:fixture:0" in str(caught.value)
    repair = str(caught.value.details["repair"])
    if result is EvidenceResult.FAIL:
        assert "enginery calibrate" not in repair
    else:
        calibration = shlex.split(repair[repair.index("enginery calibrate") :])
        assert calibration == [
            "enginery",
            "calibrate",
            str(request.run.id),
            "--project",
            str(request.repository_path),
            "--database",
            str(database),
            "--owner",
            "owner's terminal",
        ]
    retry = repair.partition(";")[0]
    command = shlex.split(retry[retry.index("enginery engine next") :])
    assert command == [
        "enginery",
        "engine",
        "next",
        "--database",
        str(database),
        "--owner",
        "owner's terminal",
        "--run-id",
        str(request.run.id),
        "--advance",
    ]
