"""Recovery of an isolated run proves the runner container gone through the run's own engine."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from enginery.adapters.oci.launch import container_name_for
from enginery.cli import stage1, stage1_runner
from enginery.domain.runner import RunnerSelection
from enginery.ledger.service import LedgerService
from tests.workflows.test_stage1_isolated import (
    HEARTBEAT,
    NOW,
    ScriptedAdmission,
    _build,
    _isolated,
)


class _StubEngine:
    existing: ClassVar[set[str]] = set()

    def __init__(self, *, context: str | None = None) -> None:
        self.context = context

    def container_exists(self, name: str) -> bool:
        return name in type(self).existing


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubEngine.existing = set()
    monkeypatch.setattr(stage1_runner, "OciEngine", _StubEngine)


def test_a_trusted_local_run_has_no_container_lookup() -> None:
    assert stage1_runner.container_gone_for(RunnerSelection.trusted_local()) is None


def test_an_isolated_runs_lookup_reports_the_named_container_present_or_gone() -> None:
    gone = stage1_runner.container_gone_for(_isolated())
    assert gone is not None
    _StubEngine.existing = {container_name_for("operation-1")}

    assert gone("operation-1") is False
    assert gone("operation-2") is True


def test_the_runtime_for_an_isolated_run_is_wired_with_the_engine_lookup(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=ScriptedAdmission())
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    runtime = stage1._runtime_for_run(
        ledger_service, owner="coordinator", run_id=str(built.request.run.id)
    )

    assert runtime._container_gone is not None
    _StubEngine.existing = {container_name_for("operation-9")}
    assert runtime._container_gone("operation-9") is False


def test_a_run_that_is_not_a_stage1_run_gets_the_plain_runtime(
    ledger_service: LedgerService,
) -> None:
    runtime = stage1._runtime_for_run(ledger_service, owner="coordinator", run_id="unknown")

    assert runtime._container_gone is None
