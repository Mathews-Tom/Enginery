"""Tests for the ``enginery outcome`` CLI command family."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.cli.main import main
from enginery.domain.ids import ObservationId, OutcomeId, RunId, WorkItemId
from enginery.domain.outcome import OutcomeKind
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.ledger.service import LedgerService

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
