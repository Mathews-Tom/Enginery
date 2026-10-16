from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.adapters.oci.launch import container_name_for
from enginery.cli import factory_control, factory_run
from enginery.cli.factory_collect import database_path, paused_hold
from enginery.cli.main import main
from enginery.domain.errors import MissingPrerequisiteError
from enginery.domain.ids import RunId
from enginery.domain.runner import RunnerProfile, RunnerSelection
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import stage1_request_from_state
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


def test_changed_snapshot_repairs_execute_with_live_implementation(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, run = dispatch(bound_project, capsys)
    _, github = bound_project
    try:
        github.serve(updated_at="2026-10-17T00:00:00Z")
        code, held = invoke(capsys, project, "run", "owner/repo#1", "--once")
        assert code == 5
        commands = held["holds"][0]["resolve"].split("; ")
        assert commands[0].startswith("enginery factory drain --stop ")
        assert all("--node-id implement" not in command for command in commands)
        assert all("--node-id qualify" not in command for command in commands)
        for command in commands:
            capsys.readouterr()
            assert main(shlex.split(command)[1:]) == 0
            capsys.readouterr()
        _, old = invoke(capsys, project, "status", run["run_id"])
        assert old["superseded_by"] is not None
    finally:
        invoke(capsys, project, "drain", "--stop")


def test_collect_paused_project_reports_factory_pause_repair(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    code, paused = invoke(capsys, project, "pause")
    assert code == 0 and paused["paused"]
    code, held = invoke(capsys, project, "collect")
    assert code == 5
    dispatch_hold = next(hold for hold in held["holds"] if hold["prerequisite"] == "dispatch")
    assert dispatch_hold["resolve"] == f"enginery factory pause --lift --project {project}"


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
    with contextlib.suppress(ChildProcessError):
        os.waitpid(run["worker"]["pid"], 0)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, observed = invoke(capsys, project, "status", run["run_id"])
        if observed["worker"]["liveness"] != "alive":
            break
        time.sleep(0.05)
    try:
        recovered = {}
        for _ in range(3):
            _, recovered = invoke(capsys, project, "resume", run["run_id"])
            ledger = LedgerService.open(Path(run["database"]))
            try:
                worker = ledger.read_process_manager_state(
                    process_manager_name="worker-supervisor", state_key=run["run_id"] + ":implement"
                )
                assert worker is not None
                if worker.state["status"] == "exit_reconciled":
                    break
                assert recovered["action_taken"] == "reconcile_worker"
            finally:
                ledger.close()
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
    assert not database_path(project).exists()


@pytest.mark.parametrize(
    ("isolated", "docker_context", "removal_fails"),
    [
        (True, "bound-worker-context", False),
        (True, None, False),
        (False, None, False),
        (True, "bound-worker-context", True),
    ],
)
def test_drain_resolves_container_cleanup_from_each_workers_bound_request(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    isolated: bool,
    docker_context: str | None,
    removal_fails: bool,
) -> None:
    project, _ = bound_project
    _, bound = invoke(capsys, project, "run", "owner/repo#1", "--once")
    request = stage1_request_from_state(json.loads(Path(bound["request_path"]).read_text()))
    runner = (
        RunnerSelection.isolated(
            image="sha256:" + "ab" * 32,
            profile=RunnerProfile(),
            docker_context=docker_context,
        )
        if isolated
        else RunnerSelection.trusted_local()
    )
    request = replace(
        request,
        run=replace(request.run, id=RunId("cleanup-run")),
        runner=runner,
        workspace_path=request.workspace_path.with_name("cleanup-workspace"),
    )
    ledger = LedgerService.open(Path(bound["database"]))
    runtime = CoordinatorRuntime(ledger, owner="factory")
    worker_group = None
    try:
        now = datetime.now(UTC)
        runtime.register_run(
            run_id=str(request.run.id),
            initial_state=request.initial_state(),
            now=now,
            heartbeat_window=timedelta(seconds=120),
        )
        tick = runtime.tick(
            now=now,
            heartbeat_window=timedelta(seconds=120),
            lease_window=timedelta(seconds=120),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            requests=(
                FixtureDispatch(
                    run_id=str(request.run.id),
                    node_id="implement",
                    attempt_id="cleanup-attempt",
                    repository_id=request.repository_id,
                    repository_path=project,
                    workspace_path=request.workspace_path,
                    base_revision=request.run.base_revision,
                    command=(sys.executable, "-c", "import time; time.sleep(60)"),
                    expected_attempt_version=0,
                    operation_id="cleanup-operation",
                ),
            ),
        )
        assert len(tick.dispatched) == 1
        worker_group = tick.dispatched[0].identity.process_group_id
    finally:
        ledger.close()

    contexts: list[str | None] = []
    removed: list[str] = []

    class Engine:
        exists = True

        def container_exists(self, name: str) -> bool:
            assert name == container_name_for("cleanup-operation")
            return self.exists

        def remove(self, name: str, *, force: bool = True) -> None:
            assert force
            removed.append(name)
            if removal_fails:
                raise MissingPrerequisiteError("container removal refused")
            self.exists = False

    engine = Engine()

    def engine_factory(*, context: str | None) -> Engine:
        contexts.append(context)
        return engine

    monkeypatch.setattr(factory_control, "OciEngine", engine_factory)
    try:
        code, drained = invoke(capsys, project, "drain", "--stop")
        cleanup = drained["container_cleanup"]
        assert len(cleanup) == 1
        assert cleanup[0]["run_id"] == str(request.run.id)
        assert drained["stopped"][0]["stopped"] and drained["remaining"] == []
        assert contexts == ([docker_context] if isolated else [])
        assert removed == ([container_name_for("cleanup-operation")] if isolated else [])
        if removal_fails:
            assert code == 5 and drained["released"].startswith("partial")
            assert cleanup[0]["status"] == "failed"
            assert cleanup[0]["error"] == "container removal refused"
            assert drained["stopped"][0]["container_error"] == "container removal refused"
        else:
            assert code == 0 and drained["released"] == "now"
            assert cleanup[0]["status"] == (
                "performed" if isolated else "skipped: trusted-local run has no container"
            )
            assert cleanup[0]["containers_removed"] == (
                [container_name_for("cleanup-operation")] if isolated else []
            )
    finally:
        if worker_group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(worker_group, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(tick.dispatched[0].identity.pid, 0)
