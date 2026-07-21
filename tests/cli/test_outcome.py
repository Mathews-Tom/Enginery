"""Tests for the ``enginery outcome`` CLI command family."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.cli.main import main
from enginery.domain.ids import ObservationId, OutcomeId, RunId, WorkItemId
from enginery.domain.outcome import OutcomeKind
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest

_OPENED = datetime(2026, 1, 1, tzinfo=UTC)


def _seed(database: Path) -> tuple[ObservationId, ObservationId]:
    service = OutcomeCaptureService(ledger=LedgerService.open(database))
    try:
        captured_source = service.register_pending(
            work_item_id=WorkItemId("wi-1"),
            run_id=RunId("run-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-1",
            opened_at=_OPENED,
        )
        service.capture(
            captured_source.id,
            outcome_id=OutcomeId("outcome-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-2"),
        )
        pending = service.register_pending(
            work_item_id=WorkItemId("wi-3"),
            run_id=RunId("run-2"),
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )
        return captured_source.id, pending.id
    finally:
        service.ledger.close()


def test_outcome_list_reports_every_observation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    _seed(database)

    exit_code = main(["outcome", "list", "--database", str(database)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {observation["id"] for observation in payload["observations"]} == {
        "run-1:escaped_defect",
        "run-2:merge_result",
    }


def test_outcome_list_filters_by_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "ledger.db"
    _seed(database)

    exit_code = main(["outcome", "list", "--database", str(database), "--state", "pending"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [observation["id"] for observation in payload["observations"]] == ["run-2:merge_result"]


def test_outcome_show_reports_a_captured_observation_with_its_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    captured_id, _ = _seed(database)

    exit_code = main(["outcome", "show", "--database", str(database), str(captured_id)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    assert payload["state"] == "captured"
    assert payload["outcome"]["id"] == "outcome-1"
    assert payload["outcome"]["kind"] == "escaped_defect"


def test_outcome_show_reports_not_found_for_an_unregistered_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    _seed(database)

    exit_code = main(["outcome", "show", "--database", str(database), "missing"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"observation_id": "missing", "found": False}


def test_outcome_completeness_reports_the_versioned_derivation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    _seed(database)

    exit_code = main(["outcome", "completeness", "--database", str(database)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "derivation_version": 1,
        "captured": 1,
        "indeterminate": 0,
        "pending": 1,
        "completeness": 1.0,
    }


def test_outcome_requires_a_subcommand(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    exit_code = main(["outcome"])

    assert exit_code != 0


def test_outcome_interventions_reports_a_recorded_human_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    runtime = CoordinatorRuntime(ledger, owner="cli-test")
    manifest = issue_to_pr_manifest()
    request = FixtureDispatch(
        run_id="run-1",
        node_id="qualify",
        attempt_id="qualify-0",
        repository_id="repo-1",
        repository_path=tmp_path,
        workspace_path=tmp_path,
        base_revision="base",
        command=("qualify",),
        expected_attempt_version=0,
        operation_id="op-1",
        dependencies=(),
        workflow_definition_id=manifest.id.value,
    )
    epoch = runtime.register_node(
        dispatch=WorkflowNodeDispatch(request, manifest),
        now=_OPENED,
        heartbeat_window=timedelta(seconds=60),
    )
    runtime.await_human_node(
        run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=_OPENED, reason="needs review"
    )
    runtime.resolve_human_wait(
        run_id="run-1",
        node_id="qualify",
        epoch=epoch.epoch,
        now=_OPENED,
        outcome="passed",
        extra={"operator_decision": "approved", "reason": "looks good"},
    )
    ledger.close()

    exit_code = main(["outcome", "interventions", "--database", str(database), "--run-id", "run-1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "interventions": [
            {
                "run_id": "run-1",
                "node_id": "qualify",
                "decision": "approved",
                "reason": "looks good",
                "status": "passed",
            }
        ]
    }


def test_outcome_failures_reports_a_failed_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    runtime = CoordinatorRuntime(ledger, owner="cli-test")
    manifest = issue_to_pr_manifest()
    request = FixtureDispatch(
        run_id="run-1",
        node_id="qualify",
        attempt_id="qualify-0",
        repository_id="repo-1",
        repository_path=tmp_path,
        workspace_path=tmp_path,
        base_revision="base",
        command=("qualify",),
        expected_attempt_version=0,
        operation_id="op-1",
        dependencies=(),
        workflow_definition_id=manifest.id.value,
    )
    epoch = runtime.register_node(
        dispatch=WorkflowNodeDispatch(request, manifest),
        now=_OPENED,
        heartbeat_window=timedelta(seconds=60),
    )
    runtime.complete_node(
        run_id="run-1",
        node_id="qualify",
        epoch=epoch.epoch,
        now=_OPENED,
        outcome="failed",
        extra={"failure_reason": "issue no longer qualifies"},
    )
    ledger.close()

    exit_code = main(["outcome", "failures", "--database", str(database), "--run-id", "run-1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["failures"]) == 1
    failure = payload["failures"][0]
    assert failure["run_id"] == "run-1"
    assert failure["node_id"] == "qualify"
    assert failure["detail"]["failure_reason"] == "issue no longer qualifies"
