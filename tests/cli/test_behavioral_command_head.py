"""Standalone check/calibrate use the verified attempt, including bare intakes."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.cli import factory_behavioral
from enginery.domain.errors import HumanActionRequiredError
from enginery.domain.runner import RunnerProfile, RunnerSelection
from enginery.engine.task_repository import intake_path_for
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.run_lifecycle import Stage1RunService
from tests.evidence.test_behavioral_acceptance import _repository
from tests.workflows.test_stage1_behavioral import _ready_to_verify


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("calibrate", [False, True])
def test_behavioral_commands_select_verified_attempt_not_intake_head(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated: bool,
    calibrate: bool,
) -> None:
    request, service, _ = _ready_to_verify(ledger_service, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    checkout, head = _repository(source, "print('changed')\n")
    bound = replace(request, workspace_path=checkout)
    if isolated:
        bound = replace(
            bound,
            runner=RunnerSelection.isolated(image="sha256:" + "a" * 64, profile=RunnerProfile()),
        )
        intake = intake_path_for(checkout)
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=enginery-intake", str(intake)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(intake),
                "fetch",
                str(checkout),
                f"HEAD:refs/heads/{request.head_branch}",
            ],
            check=True,
            capture_output=True,
        )
        assert (
            subprocess.run(
                ["git", "-C", str(intake), "rev-parse", "--verify", "HEAD"], capture_output=True
            ).returncode
            != 0
        )
    current = ledger_service.read_projection(
        aggregate_type="node_attempt", aggregate_id=request.implementation.attempt_id
    )
    assert current is not None
    ledger_service.append(
        AppendCommand(
            correlation_id="record-real-candidate",
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=request.implementation.attempt_id,
                    expected_version=current.aggregate_version,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={"result": {"head_revision": head}},
                ),
            ),
        )
    )
    loaded = replace(service.read(request.run.id), request=bound)
    monkeypatch.setattr(Stage1RunService, "read", lambda *args: loaded)
    calls: list[tuple[str, bool]] = []

    def execute(
        ledger: LedgerService,
        request: object,
        *,
        head_revision: str,
        calibrate: bool,
        without_faults: tuple[str, ...],
    ) -> dict[str, object]:
        calls.append((head_revision, calibrate))
        return {"head_revision": head_revision}

    monkeypatch.setattr(factory_behavioral, "execute_behavioral", execute)
    database = Path(ledger_service.connection.execute("PRAGMA database_list").fetchone()[2])
    result = factory_behavioral.run_command(
        checkout, str(request.run.id), calibrate=calibrate, database=database, owner="operator"
    )
    assert result["head_revision"] == head
    assert calls == [(head, calibrate)]


def test_check_refuses_unverified_workspace_head(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, service, _ = _ready_to_verify(ledger_service, tmp_path)
    current = ledger_service.read_projection(
        aggregate_type="node_attempt", aggregate_id=request.implementation.attempt_id
    )
    assert current is not None
    ledger_service.append(
        AppendCommand(
            correlation_id="unverified-candidate",
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=request.implementation.attempt_id,
                    expected_version=current.aggregate_version,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={"result": {}},
                ),
            ),
        )
    )
    loaded = service.read(request.run.id)
    monkeypatch.setattr(Stage1RunService, "read", lambda *args: loaded)
    database = Path(ledger_service.connection.execute("PRAGMA database_list").fetchone()[2])
    with pytest.raises(HumanActionRequiredError, match="verified implementation attempt head"):
        factory_behavioral.run_command(
            request.repository_path, str(request.run.id), calibrate=False, database=database
        )
