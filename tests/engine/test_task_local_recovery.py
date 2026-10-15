"""Recovery never runs git inside a worker-writable task-local clone.

The worker of an isolated run controls the clone's git configuration. These tests play that
worker with a real subprocess that plants ``core.fsmonitor`` (an executable that records a
marker file and exits non-zero) and then drive every recovery path of the real coordinator
runtime -- worker failure, cancel, human wait and agent retry -- asserting the marker never
appears. A control test proves the planted configuration really executes when git is run in the
clone, so the absence of the marker is meaningful.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.engine.recovery import (
    RecoveryAssessment,
    assess_workspace_quiescence,
)
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import CoordinatorRuntime, DispatchedFixture, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.workspace import (
    WORKSPACE_MODE_GIT_WORKTREE,
    WORKSPACE_MODE_TASK_LOCAL_CLONE,
)
from enginery.ledger.service import LedgerService

_NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=60)
_LEASE = timedelta(seconds=600)
_ONE_SLOT = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_NO_GLOBAL_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

# Plants core.fsmonitor in the clone, signals readiness, then sleeps or exits.
_PLANTER = textwrap.dedent(
    """
    import os, pathlib, subprocess, sys, time

    marker, behaviour = sys.argv[1:3]
    ws = pathlib.Path.cwd()
    trap = ws / "fsmonitor-trap.sh"
    trap.write_text("#!/bin/sh\\necho fsmonitor >> " + marker + "\\nexit 1\\n")
    trap.chmod(0o755)
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    subprocess.run(["git", "config", "core.fsmonitor", str(trap)], cwd=ws, check=True, env=env)
    (ws / "planted").write_text("1")
    if behaviour == "sleep":
        time.sleep(120)
    """
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_NO_GLOBAL_GIT},
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", "-q", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-q", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _request(
    tmp_path: Path,
    repository: Path,
    base: str,
    *,
    marker: Path,
    behaviour: str,
    attempt: int = 1,
    retain: bool = False,
    mode: str = WORKSPACE_MODE_TASK_LOCAL_CLONE,
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id="run-1",
        node_id="implement",
        attempt_id=f"attempt-{attempt}",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspaces" / "run-1",
        base_revision=base,
        command=(sys.executable, "-c", _PLANTER, str(marker), behaviour),
        expected_attempt_version=0,
        operation_id=f"operation-{attempt}",
        retain_workspace=retain,
        workspace_mode=mode,
    )


def _tick(
    runtime: CoordinatorRuntime, now: datetime, *requests: FixtureDispatch
) -> tuple[DispatchedFixture, ...]:
    return runtime.tick(
        now=now,
        heartbeat_window=_WINDOW,
        lease_window=_LEASE,
        limits=_ONE_SLOT,
        requests=requests,
    ).dispatched


def _wait_planted(workspace: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (workspace / "planted").exists():
            return
        time.sleep(0.02)
    raise AssertionError("the simulated worker never planted its configuration")


def _reap(dispatched: DispatchedFixture) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(dispatched.identity.pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped == dispatched.identity.pid:
            return
        time.sleep(0.02)
    raise AssertionError("the supervised worker did not exit")


def _node_status(ledger: LedgerService) -> object:
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id="run-1:implement")
    assert node is not None
    return node.state["status"]


def test_the_planted_fsmonitor_really_runs_when_git_is_run_in_the_clone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    """Control: without the guard the marker appears, so its absence below proves the guard."""
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    (running,) = _tick(
        runtime, _NOW, _request(tmp_path, repository, base, marker=marker, behaviour="sleep")
    )
    workspace = running.workspace.workspace_path
    _wait_planted(workspace)

    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        env={**os.environ, **_NO_GLOBAL_GIT},
        capture_output=True,
        check=False,
    )
    assert marker.exists(), "the control must show the planted fsmonitor executing"

    marker.unlink()
    epoch = runtime.claim_epoch(now=_NOW + timedelta(seconds=1), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-1", node_id="implement", epoch=epoch.epoch, now=_NOW + timedelta(seconds=2)
    )
    assert not marker.exists()


def test_a_dead_worker_is_failed_without_running_git_in_its_clone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    (running,) = _tick(
        runtime,
        _NOW,
        _request(tmp_path, repository, base, marker=marker, behaviour="exit", retain=True),
    )
    _wait_planted(running.workspace.workspace_path)
    _reap(running)

    runtime.fail_exited_worker(
        run_id="run-1",
        node_id="implement",
        now=_NOW + timedelta(seconds=2),
        heartbeat_window=_WINDOW,
    )

    assert _node_status(ledger_service) == "failed"
    assert not marker.exists()
    assert (running.workspace.workspace_path / "planted").exists(), "the clone is retained"


def test_a_cancelled_worker_is_reconciled_without_running_git_in_its_clone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    (running,) = _tick(
        runtime,
        _NOW,
        _request(tmp_path, repository, base, marker=marker, behaviour="sleep", retain=True),
    )
    _wait_planted(running.workspace.workspace_path)
    epoch = runtime.claim_epoch(now=_NOW + timedelta(seconds=1), heartbeat_window=_WINDOW)

    runtime.cancel_node(
        run_id="run-1", node_id="implement", epoch=epoch.epoch, now=_NOW + timedelta(seconds=2)
    )

    assert _node_status(ledger_service) == "cancelled"
    assert not marker.exists()


def test_a_human_wait_is_entered_without_running_git_in_the_clone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    (running,) = _tick(
        runtime, _NOW, _request(tmp_path, repository, base, marker=marker, behaviour="sleep")
    )
    _wait_planted(running.workspace.workspace_path)

    runtime.enter_human_wait(
        dispatched=running, reason="operator review", now=_NOW + timedelta(seconds=1)
    )

    assert _node_status(ledger_service) == "awaiting_human"
    assert not marker.exists()
    reservation = runtime.read_workspace_reservation("repository-1")
    assert reservation is not None and reservation.status == "retained"


def test_an_agent_retry_reconciles_the_prior_clone_without_running_git_in_it(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    (running,) = _tick(
        runtime,
        _NOW,
        _request(tmp_path, repository, base, marker=marker, behaviour="exit", retain=True),
    )
    _wait_planted(running.workspace.workspace_path)
    _reap(running)
    runtime.ingest_result(
        envelope=WorkerResultEnvelope(
            run_id="run-1",
            node_id="implement",
            attempt_id="attempt-1",
            epoch=running.lease.epoch,
            fencing_token=running.lease.fencing_token,
            operation_id="operation-1",
            terminal_result="failed",
            artifact_references=(),
            result={},
        ),
        now=_NOW + timedelta(seconds=1),
    )
    assert _node_status(ledger_service) == "failed"
    retry = _request(
        tmp_path, repository, base, marker=marker, behaviour="exit", attempt=2, retain=True
    )

    (second,) = _tick(runtime, _NOW + timedelta(seconds=2), retry)

    assert second.lease.attempt_id == "attempt-2"
    assert not marker.exists()
    _wait_planted(second.workspace.workspace_path)
    _reap(second)


def test_task_local_quiescence_runs_no_git_and_checks_the_lock_without_following_links(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed.marker"
    workspace = tmp_path / "ws"
    _git("init", "-q", str(workspace), cwd=tmp_path)
    trap = tmp_path / "trap.sh"
    trap.write_text(f"#!/bin/sh\necho fsmonitor >> {marker}\nexit 1\n", encoding="utf-8")
    trap.chmod(0o755)
    _git("config", "core.fsmonitor", str(trap), cwd=workspace)

    quiescent = assess_workspace_quiescence(
        workspace, workspace_mode=WORKSPACE_MODE_TASK_LOCAL_CLONE
    )
    assert quiescent == RecoveryAssessment(True, "process_absent_workspace_quiescent")
    assert not marker.exists()

    # A lock, even a dangling symlink named index.lock, is a lock: it is never followed.
    (workspace / ".git" / "index.lock").symlink_to(tmp_path / "nowhere")
    locked = assess_workspace_quiescence(workspace, workspace_mode=WORKSPACE_MODE_TASK_LOCAL_CLONE)
    assert locked == RecoveryAssessment(False, "workspace_git_lock_present")
    (workspace / ".git" / "index.lock").unlink()

    # A `.git` swapped for a symlink or file is not a real git directory and holds recovery.
    real = workspace / ".git"
    moved = tmp_path / "moved-git"
    real.rename(moved)
    real.symlink_to(moved)
    swapped = assess_workspace_quiescence(workspace, workspace_mode=WORKSPACE_MODE_TASK_LOCAL_CLONE)
    assert swapped == RecoveryAssessment(False, "workspace_identity_unreadable")
    assert not marker.exists()

    missing = assess_workspace_quiescence(
        tmp_path / "absent", workspace_mode=WORKSPACE_MODE_TASK_LOCAL_CLONE
    )
    assert missing == RecoveryAssessment(False, "workspace_missing")


def test_a_present_runner_container_blocks_task_local_recovery(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    seen: list[str] = []

    def container_gone(operation_id: str) -> bool:
        seen.append(operation_id)
        return False

    runtime = CoordinatorRuntime(ledger_service, owner="coordinator", container_gone=container_gone)
    (running,) = _tick(
        runtime,
        _NOW,
        _request(tmp_path, repository, base, marker=marker, behaviour="exit", retain=True),
    )
    _wait_planted(running.workspace.workspace_path)
    _reap(running)

    runtime.fail_exited_worker(
        run_id="run-1",
        node_id="implement",
        now=_NOW + timedelta(seconds=2),
        heartbeat_window=_WINDOW,
    )

    assert seen == ["operation-1"]
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:implement"
    )
    assert node is not None
    assert node.state["status"] == "awaiting_human"
    assert node.state["cancellation_cleanup_reason"] == "runner_container_present"
    assert not marker.exists()


def test_worktree_recovery_still_inspects_the_workspace_with_hardened_git(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    """Linked worktrees keep their git-based quiescence check, with fsmonitor and hooks off."""
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    request = _request(
        tmp_path,
        repository,
        base,
        marker=marker,
        behaviour="exit",
        retain=True,
        mode=WORKSPACE_MODE_GIT_WORKTREE,
    )
    (running,) = _tick(runtime, _NOW, request)
    workspace = running.workspace.workspace_path
    _wait_planted(workspace)
    _reap(running)

    runtime.fail_exited_worker(
        run_id="run-1",
        node_id="implement",
        now=_NOW + timedelta(seconds=2),
        heartbeat_window=_WINDOW,
    )

    assert _node_status(ledger_service) == "failed"
    assert not marker.exists(), "host git in a worktree runs with core.fsmonitor disabled"
