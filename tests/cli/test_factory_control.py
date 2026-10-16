from __future__ import annotations

import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.cli import factory_run
from enginery.cli.factory_collect import paused_hold
from enginery.cli.main import main
from enginery.ledger.service import LedgerService
from tests.cli.test_factory_run import bound_project as bound_project
from tests.cli.test_factory_run import invoke
from tests.support.github import StandInGitHub


def dispatch(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, dict]:
    project, github = bound_project
    harness = github.executable.parent.parent / "omp"
    harness.write_text(
        f"#!{sys.executable}\nimport json, sys, time\n"
        "if '--help' in sys.argv:\n print('omp v18.10.3')\n"
        "else:\n time.sleep(60)\n",
        encoding="utf-8",
    )
    _, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    code, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0 and run["worker"]["pid"] > 0
    return project, run


def test_pause_holds_resume_and_run_then_drain_stops_and_releases_resources(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run = dispatch(bound_project, capsys)
    try:
        code, paused = invoke(capsys, project, "pause", "--reason", "operator maintenance")
        assert code == 0 and paused["paused"]
        assert paused["scope"] == f"project ledger {run['database']}"
        code, held = invoke(capsys, project, "resume", run["run_id"])
        assert code == 5
        assert held["holds"][0]["resolve"] == f"enginery factory pause --lift --project {project}"
        assert held["worker"]["pid"] == run["worker"]["pid"]
        monkeypatch.setattr(
            factory_run,
            "intake_prerequisites",
            lambda configuration: paused_hold(configuration.project),
        )
        code, held = invoke(capsys, project, "run", "owner/repo#2")
        assert code == 5 and "pause --lift" in held["holds"][0]["resolve"]
        code, drained = invoke(capsys, project, "drain", "--stop")
        assert code == 0 and drained["drained"] and drained["paused"]
        assert drained["released"] == "now"
        assert drained["stopped"][0]["stopped"]
        assert not Path(run["request_path"]).with_name("workspace").exists()
        ledger = LedgerService.open(Path(run["database"]))
        try:
            supervisor = ledger.read_process_manager_state(
                process_manager_name="worker-supervisor", state_key=run["run_id"] + ":implement"
            )
            assert supervisor is not None and supervisor.state["status"] == "exit_reconciled"
            lease = ledger.read_lease(run_id=run["run_id"], node_id="implement")
            assert lease is not None and lease.expires_at is not None
            assert datetime.fromisoformat(lease.expires_at) <= datetime.now(UTC)
            reservations = ledger.list_process_manager_states(
                process_manager_name="workspace-reservations"
            )
            assert reservations and all(item.state["status"] == "cleaned" for item in reservations)
        finally:
            ledger.close()
        code, lifted = invoke(capsys, project, "pause", "--lift")
        assert code == 0 and not lifted["paused"]
    finally:
        invoke(capsys, project, "drain", "--stop")


def test_resume_reconciles_a_killed_worker_without_new_request_or_workspace(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, run = dispatch(bound_project, capsys)
    request = Path(run["request_path"])
    original = request.read_bytes()
    os.killpg(run["worker"]["process_group_id"], signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, observed = invoke(capsys, project, "status", run["run_id"])
        if observed["worker"]["liveness"] != "alive":
            break
        time.sleep(0.05)
    try:
        _, recovered = invoke(capsys, project, "resume", run["run_id"])
        assert recovered["run_id"] == run["run_id"]
        assert recovered["request_path"] == str(request) and request.read_bytes() == original
        ledger = LedgerService.open(Path(run["database"]))
        try:
            worker = ledger.read_process_manager_state(
                process_manager_name="worker-supervisor", state_key=run["run_id"] + ":implement"
            )
            assert worker is not None and worker.state["status"] == "exit_reconciled"
        finally:
            ledger.close()
        assert len(list(request.parent.parent.glob("*/request.json"))) == 1
    finally:
        invoke(capsys, project, "drain", "--stop")


def test_resume_unknown_run_does_not_create_a_ledger(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    assert main(["factory", "resume", "unknown", "--project", str(project), "--json"]) == 2
    assert "unknown factory run" in capsys.readouterr().err
    assert not factory_run.database_path(project).exists()
