from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.cli.main import main
from enginery.domain.ids import PlanId, PlanMilestoneId, StackId
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=30)


def _seed_stack(database: Path) -> None:
    service = LedgerService.open(database)
    try:
        coordinator = StackCoordinator(service, CoordinatorRuntime(service, owner="seed"))
        coordinator.start(
            stack_id=StackId("stack-1"),
            plan_id=PlanId("plan-1"),
            base_ref="main",
            ordered_milestones=((PlanMilestoneId("m1"), "fixture/m1"),),
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
    finally:
        service.close()


def test_status_reports_an_existing_stack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    _seed_stack(database)

    exit_code = main(
        [
            "stage2",
            "status",
            "--database",
            str(database),
            "--owner",
            "test",
            "--stack-id",
            "stack-1",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["found"] is True
    assert output["stack_id"] == "stack-1"
    assert output["slices"][0]["milestone_id"] == "m1"
    assert output["slices"][0]["state"] == "pending"


def test_status_reports_a_missing_stack(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "ledger.sqlite"
    LedgerService.open(database).close()

    exit_code = main(
        [
            "stage2",
            "status",
            "--database",
            str(database),
            "--owner",
            "test",
            "--stack-id",
            "does-not-exist",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["found"] is False
