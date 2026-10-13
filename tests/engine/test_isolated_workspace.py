"""Task-local clone workspaces: self-containment, bundle intake and cleanup, with real git.

The worker of an isolated run controls everything inside its workspace, including the
repository's own configuration and hooks. These tests play that worker with a real
subprocess (what ``worker_entry`` does inside the container: commit on a branch, write a
git bundle into the out directory) and prove the host never executes anything the worker
planted.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.runtime import (
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
    FixtureRuntime,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.task_repository import (
    BUNDLE_FILE_NAME,
    WORKER_HOOKS_PATH,
    intake_path_for,
    out_path_for,
)
from enginery.engine.workspace import (
    WORKSPACE_MODE_GIT_WORKTREE,
    WORKSPACE_MODE_TASK_LOCAL_CLONE,
    GitWorktreeBackend,
)
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
BRANCH = "enginery/run-1"
REPOSITORY_ID = "repository-1"
_NO_GLOBAL_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

# The simulated in-container worker. It commits on a branch inside its clone and writes the
# result bundle into the out directory (a sibling of the workspace); then, like a hostile
# worker, it plants hooks and configuration that would execute if the host ran git in the clone.
_WORKER = textwrap.dedent(
    """
    import os, pathlib, subprocess, sys

    base, branch, marker, behaviour = sys.argv[1:5]
    os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    ws = pathlib.Path.cwd()
    out = ws.with_name(ws.name + ".out")


    def git(*args, capture=False):
        done = subprocess.run(
            ["git", *args], cwd=ws, check=True, text=True, capture_output=capture
        )
        return done.stdout.strip() if capture else ""


    def commit_file(name="feature.txt"):
        (ws / name).write_text("implemented\\n")
        git("add", name)
        git("commit", "-q", "-m", "implement")


    def plant_traps():
        trap = ws / "trap.sh"
        trap.write_text("#!/bin/sh\\necho executed >> " + marker + "\\n")
        trap.chmod(0o755)
        hooks = ws / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-push", "post-checkout", "post-merge", "reference-transaction"):
            (hooks / name).write_text("#!/bin/sh\\necho " + name + " >> " + marker + "\\n")
            (hooks / name).chmod(0o755)
        git("config", "core.hooksPath", str(hooks))
        git("config", "core.sshCommand", str(trap))
        git("config", "core.fsmonitor", str(trap))
        git("config", "core.pager", str(trap))
        git("config", "diff.external", str(trap))
        git("config", "protocol.ext.allow", "always")


    out.mkdir(exist_ok=True)
    bundle = out / "branch.bundle"
    if behaviour == "no_bundle":
        pass
    elif behaviour == "implement":
        git("checkout", "-q", "-b", branch)
        commit_file()
        git("bundle", "create", str(bundle), base + ".." + branch)
        (out / "head.txt").write_text(git("rev-parse", branch, capture=True))
    elif behaviour == "empty_commit":
        git("checkout", "-q", "-b", branch)
        git("commit", "-q", "--allow-empty", "-m", "nothing")
        git("bundle", "create", str(bundle), base + ".." + branch)
    elif behaviour == "orphan":
        git("checkout", "-q", "--orphan", branch)
        git("rm", "-rf", "-q", ".")
        commit_file("unrelated.txt")
        git("bundle", "create", str(bundle), branch)
    elif behaviour == "other_branch":
        git("checkout", "-q", "-b", "some/other-branch")
        commit_file()
        git("bundle", "create", str(bundle), base + "..some/other-branch")
    elif behaviour == "empty_bundle":
        bundle.write_bytes(b"")
    elif behaviour == "symlink_bundle":
        bundle.symlink_to("/etc/hosts")
    else:
        raise SystemExit("unknown behaviour " + behaviour)
    plant_traps()
    (out / "done").write_text("done")
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


def _plant_host_traps(repository: Path, marker: Path) -> None:
    """Executable host-side hooks and a malicious fsmonitor that must never run."""
    script = repository / ".git" / "host-trap.sh"
    script.write_text(f"#!/bin/sh\necho host-trap >> {marker}\n", encoding="utf-8")
    script.chmod(0o755)
    hooks = repository / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "post-merge", "post-commit", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n", encoding="utf-8")
        (hooks / name).chmod(0o755)
    _git("config", "core.fsmonitor", str(script), cwd=repository)
    _git("config", "core.sshCommand", str(script), cwd=repository)


def _request(
    *,
    repository: Path,
    base: str,
    workspace: Path,
    command: tuple[str, ...],
    mode: str,
    node_id: str = "implement",
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id="run-1",
        node_id=node_id,
        attempt_id="attempt-1",
        repository_id=REPOSITORY_ID,
        repository_path=repository,
        workspace_path=workspace,
        base_revision=base,
        command=command,
        expected_attempt_version=0,
        operation_id="operation-1",
        workspace_mode=mode,
    )


def _await_exit(dispatched: DispatchedFixture) -> None:
    """Reap the supervised worker (a child of this process) and require a clean exit."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            reaped, status = os.waitpid(dispatched.identity.pid, os.WNOHANG)
        except ChildProcessError:
            return  # already reaped by a later Popen in this process: the worker has exited
        if reaped == dispatched.identity.pid:
            assert os.waitstatus_to_exitcode(status) == 0
            return
        time.sleep(0.02)
    raise AssertionError("the supervised worker did not exit")


def _worker_command(base: str, marker: Path, behaviour: str) -> tuple[str, ...]:
    return (sys.executable, "-c", _WORKER, base, BRANCH, str(marker), behaviour)


def _dispatch_clone(
    ledger: LedgerService,
    *,
    repository: Path,
    base: str,
    workspace: Path,
    command: tuple[str, ...] = (sys.executable, "-c", "pass"),
) -> tuple[CoordinatorRuntime, DispatchedFixture]:
    runtime = CoordinatorRuntime(ledger, owner="coordinator")
    ticked = runtime.tick(
        now=NOW,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(
            _request(
                repository=repository,
                base=base,
                workspace=workspace,
                command=command,
                mode=WORKSPACE_MODE_TASK_LOCAL_CLONE,
            ),
        ),
    )
    assert len(ticked.dispatched) == 1
    return runtime, ticked.dispatched[0]


def _backend(ledger: LedgerService, runtime: CoordinatorRuntime) -> GitWorktreeBackend:
    return GitWorktreeBackend(ledger, runtime.coordinator)


def _epoch(runtime: CoordinatorRuntime) -> int:
    epoch = runtime.coordinator.current_epoch()
    assert epoch is not None
    return epoch.epoch


def _file_texts(root: Path, *, skip: tuple[str, ...] = ("objects",)) -> Iterator[tuple[str, bytes]]:
    for entry in sorted(root.rglob("*")):
        relative = entry.relative_to(root)
        if relative.parts[0] in skip or entry.is_symlink() or not entry.is_file():
            continue
        yield str(relative), entry.read_bytes()


# --- materialization -----------------------------------------------------------------


def test_dispatch_materializes_a_self_contained_clone_and_runs_no_host_code(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    _plant_host_traps(repository, marker)
    workspace = tmp_path / "workspaces" / "run-1"
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=NOW, heartbeat_window=timedelta(seconds=60))

    dispatched = FixtureRuntime(ledger_service, coordinator).dispatch(
        request=_request(
            repository=repository,
            base=base,
            workspace=workspace,
            command=(sys.executable, "-c", "pass"),
            mode=WORKSPACE_MODE_TASK_LOCAL_CLONE,
        ),
        epoch=epoch.epoch,
        now=NOW,
        lease_window=timedelta(seconds=30),
    )
    _await_exit(dispatched)

    git_dir = workspace / ".git"
    assert git_dir.is_dir() and not git_dir.is_symlink()
    assert (workspace / "README").read_text(encoding="utf-8") == "fixture\n"
    # Self-contained: no borrowed objects, no remote, no host-pointing path.
    assert not (git_dir / "objects" / "info" / "alternates").exists()
    assert _git("remote", cwd=workspace) == ""
    assert _git("rev-parse", "HEAD", cwd=workspace) == base
    hosts = {str(repository).encode(), str(repository.resolve()).encode()}
    leaks = [name for name, data in _file_texts(git_dir) if any(host in data for host in hosts)]
    assert leaks == []
    assert _git("config", "--local", "core.hooksPath", cwd=workspace) == WORKER_HOOKS_PATH
    assert not [
        entry
        for entry in (git_dir / "hooks").rglob("*")
        if entry.is_file() and entry.stat().st_mode & stat.S_IXUSR
    ]
    # Nothing the host repository carried (hook, fsmonitor, ssh command) ran or was copied.
    assert not marker.exists()
    config = (git_dir / "config").read_text(encoding="utf-8")
    assert "fsmonitor" not in config and "sshCommand" not in config
    # The workspace outlives its source: the clone owns every object it needs.
    assert _git("cat-file", "-t", base, cwd=workspace) == "commit"


def test_materialization_creates_a_real_out_directory_for_the_worker(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    workspace = tmp_path / "workspaces" / "run-1"

    _runtime, dispatched = _dispatch_clone(
        ledger_service, repository=repository, base=base, workspace=workspace
    )
    _await_exit(dispatched)

    out = out_path_for(workspace)
    assert out.is_dir() and not out.is_symlink()
    assert not out.is_relative_to(workspace), "the out directory is outside the workspace"
    assert list(out.iterdir()) == []


def test_a_source_that_borrows_objects_still_yields_a_self_contained_clone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    donor, base = _repository(tmp_path)
    borrower = tmp_path / "borrower"
    subprocess.run(
        ["git", "clone", "-q", "--shared", str(donor), str(borrower)],
        check=True,
        env={**os.environ, **_NO_GLOBAL_GIT},
    )
    assert (borrower / ".git" / "objects" / "info" / "alternates").is_file()
    workspace = tmp_path / "workspaces" / "run-1"

    _runtime, dispatched = _dispatch_clone(
        ledger_service, repository=borrower, base=base, workspace=workspace
    )
    _await_exit(dispatched)
    shutil.rmtree(donor)

    assert not (workspace / ".git" / "objects" / "info" / "alternates").exists()
    assert _git("rev-parse", "HEAD", cwd=workspace) == base
    assert _git("fsck", "--no-dangling", cwd=workspace) == ""


def test_the_reservation_records_the_workspace_mode_durably(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    runtime, dispatched = _dispatch_clone(
        ledger_service, repository=repository, base=base, workspace=tmp_path / "workspaces" / "w"
    )
    _await_exit(dispatched)

    reservation = _backend(ledger_service, runtime).read_reservation(REPOSITORY_ID)

    assert reservation is not None
    assert reservation.mode == WORKSPACE_MODE_TASK_LOCAL_CLONE
    assert reservation.status == "materialized"
    stored = ledger_service.read_process_manager_state(
        process_manager_name="workspace-reservations", state_key=REPOSITORY_ID
    )
    assert stored is not None
    assert stored.state["mode"] == WORKSPACE_MODE_TASK_LOCAL_CLONE
    # The persisted node request keeps the mode too, so a replacement coordinator re-derives it.
    assert (
        runtime.read_node_request(run_id="run-1", node_id="implement").workspace_mode
        == WORKSPACE_MODE_TASK_LOCAL_CLONE
    )


def test_a_reservation_persisted_without_a_mode_decodes_as_a_git_worktree(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    legacy_state = {
        "repository_id": "legacy-repository",
        "run_id": "legacy-run",
        "repository_path": str(tmp_path / "repository"),
        "workspace_path": str(tmp_path / "workspace"),
        "base_revision": "a" * 40,
        "status": "materialized",
    }
    ledger_service.append(
        AppendCommand(
            correlation_id="legacy-workspace",
            events=(
                EventWrite(
                    "workspace", "legacy-repository", 0, "workspace.materialized", 1, legacy_state
                ),
            ),
            process_manager_updates=(
                ProcessManagerStateWrite(
                    process_manager_name="workspace-reservations",
                    state_key="legacy-repository",
                    expected_version=0,
                    state=legacy_state,
                ),
            ),
        )
    )
    coordinator = Coordinator(ledger_service, owner="coordinator")

    reservation = GitWorktreeBackend(ledger_service, coordinator).read_reservation(
        "legacy-repository"
    )

    assert reservation is not None
    assert reservation.mode == WORKSPACE_MODE_GIT_WORKTREE
    assert reservation.status == "materialized"


def test_a_worktree_reservation_stores_no_mode_key_so_old_state_is_byte_identical(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=NOW, heartbeat_window=timedelta(seconds=60))
    backend = GitWorktreeBackend(ledger_service, coordinator)

    reserved = backend.reserve(
        repository_id=REPOSITORY_ID,
        run_id="run-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace",
        base_revision=base,
        epoch=epoch.epoch,
        now=NOW,
    )

    assert reserved.mode == WORKSPACE_MODE_GIT_WORKTREE
    stored = ledger_service.read_process_manager_state(
        process_manager_name="workspace-reservations", state_key=REPOSITORY_ID
    )
    assert stored is not None
    assert "mode" not in stored.state
    assert backend.read_reservation(REPOSITORY_ID) == reserved


# --- bundle intake -------------------------------------------------------------------


def test_the_implementation_branch_is_verified_from_a_host_owned_intake_repository(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    workspace = tmp_path / "workspaces" / "run-1"
    runtime, dispatched = _dispatch_clone(
        ledger_service,
        repository=repository,
        base=base,
        workspace=workspace,
        command=_worker_command(base, marker, "implement"),
    )
    _await_exit(dispatched)
    # The worker really planted traps that fire if anyone runs git in its repository.
    assert (workspace / ".git" / "hooks" / "pre-push").stat().st_mode & stat.S_IXUSR
    planted = (workspace / ".git" / "config").read_text(encoding="utf-8")
    assert "sshCommand" in planted and "fsmonitor" in planted
    worker_head = (out_path_for(workspace) / "head.txt").read_text(encoding="utf-8")

    head = runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)

    assert head == worker_head
    assert head != base
    assert not marker.exists()
    intake = intake_path_for(workspace)
    assert intake.is_dir()
    assert _git("--git-dir", str(intake), "rev-parse", f"refs/heads/{BRANCH}", cwd=tmp_path) == head
    assert (
        _git("--git-dir", str(intake), "show", f"{head}:feature.txt", cwd=tmp_path) == "implemented"
    )
    assert _git("--git-dir", str(intake), "rev-parse", f"{head}^", cwd=tmp_path) == base
    # The intake repository is host-owned and hook-free; the source repository is untouched.
    assert _git("--git-dir", str(intake), "config", "core.hooksPath", cwd=tmp_path) == "/dev/null"
    assert _git("branch", "--list", BRANCH, cwd=repository) == ""
    # Verification is repeatable.
    assert runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH) == head


def test_only_the_requested_branch_is_imported_from_the_bundle(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    workspace = tmp_path / "workspaces" / "run-1"
    runtime, dispatched = _dispatch_clone(
        ledger_service,
        repository=repository,
        base=base,
        workspace=workspace,
        command=_worker_command(base, tmp_path / "marker", "other_branch"),
    )
    _await_exit(dispatched)

    with pytest.raises(ExternalConflictError, match="worker bundle intake failed"):
        runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)


@pytest.mark.parametrize(
    ("behaviour", "message"),
    [
        ("no_bundle", "worker bundle is missing"),
        ("empty_bundle", "worker bundle is empty"),
        ("symlink_bundle", "must be a regular file"),
        ("orphan", "does not descend from its reserved base revision"),
        ("empty_commit", "contains no implementation change"),
    ],
)
def test_verification_rejects_a_worker_result_that_is_not_a_change_on_the_base(
    ledger_service: LedgerService, tmp_path: Path, behaviour: str, message: str
) -> None:
    repository, base = _repository(tmp_path)
    marker = tmp_path / "executed.marker"
    runtime, dispatched = _dispatch_clone(
        ledger_service,
        repository=repository,
        base=base,
        workspace=tmp_path / "workspaces" / "run-1",
        command=_worker_command(base, marker, behaviour),
    )
    _await_exit(dispatched)

    with pytest.raises(ExternalConflictError, match=message):
        runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)

    assert not marker.exists()


def test_a_corrupt_bundle_is_refused_before_any_ref_is_imported(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    workspace = tmp_path / "workspaces" / "run-1"
    runtime, dispatched = _dispatch_clone(
        ledger_service,
        repository=repository,
        base=base,
        workspace=workspace,
        command=_worker_command(base, tmp_path / "marker", "no_bundle"),
    )
    _await_exit(dispatched)
    (out_path_for(workspace) / BUNDLE_FILE_NAME).write_bytes(b"this is not a git bundle")

    with pytest.raises(ExternalConflictError, match="worker bundle verification failed"):
        runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)

    assert (
        _git("--git-dir", str(intake_path_for(workspace)), "branch", "--list", cwd=tmp_path) == ""
    )


# --- cleanup -------------------------------------------------------------------------


def test_cleanup_removes_the_workspace_the_out_directory_and_the_intake_repository(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    workspace = tmp_path / "workspaces" / "run-1"
    runtime, dispatched = _dispatch_clone(
        ledger_service,
        repository=repository,
        base=base,
        workspace=workspace,
        command=_worker_command(base, tmp_path / "marker", "implement"),
    )
    _await_exit(dispatched)
    runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)
    # A hostile worker's leftovers: a mode-000 directory and a read-only file inside it.
    stuck = workspace / "stuck"
    stuck.mkdir()
    (stuck / "locked.txt").write_text("x", encoding="utf-8")
    (stuck / "locked.txt").chmod(0o400)
    stuck.chmod(0o000)
    assert (
        workspace.is_dir()
        and out_path_for(workspace).is_dir()
        and intake_path_for(workspace).is_dir()
    )

    cleaned = _backend(ledger_service, runtime).cleanup(
        dispatched.workspace, epoch=_epoch(runtime), now=NOW + timedelta(seconds=5)
    )

    assert cleaned.status == "cleaned"
    assert cleaned.mode == WORKSPACE_MODE_TASK_LOCAL_CLONE
    assert not workspace.exists()
    assert not out_path_for(workspace).exists()
    assert not intake_path_for(workspace).exists()
    # The host's own repository and everything beside the workspace is untouched.
    assert (repository / "README").is_file()
    assert (workspace.parent).is_dir()
    stored = _backend(ledger_service, runtime).read_reservation(REPOSITORY_ID)
    assert stored is not None and stored.status == "cleaned"


@contextmanager
def _read_only(directory: Path) -> Iterator[None]:
    original = directory.stat().st_mode
    directory.chmod(0o555)
    try:
        yield
    finally:
        directory.chmod(stat.S_IMODE(original))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_cleanup_that_cannot_remove_a_path_retains_the_reservation_and_names_the_path(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "run-1"
    runtime, dispatched = _dispatch_clone(
        ledger_service, repository=repository, base=base, workspace=workspace
    )
    _await_exit(dispatched)
    backend = _backend(ledger_service, runtime)
    nested = workspace / "nested" / "deep"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x", encoding="utf-8")
    nested.chmod(0o000)

    with _read_only(workspaces):
        with pytest.raises(ExternalConflictError, match="retained for reconciliation") as caught:
            backend.cleanup(
                dispatched.workspace, epoch=_epoch(runtime), now=NOW + timedelta(seconds=5)
            )
        retained_paths = str(caught.value.details["retained_paths"])
        assert str(workspace) in retained_paths
        assert caught.value.details["workspace_path"] == str(workspace)
        stored = backend.read_reservation(REPOSITORY_ID)
        assert stored is not None and stored.status == "retained"
        # What could be removed was removed; the entry that could not be is still there.
        assert workspace.is_dir()
        assert not (workspace / "nested").exists()

    # Once the operator repairs the permission, the retained reservation cleans up.
    cleaned = backend.cleanup(stored, epoch=_epoch(runtime), now=NOW + timedelta(seconds=10))
    assert cleaned.status == "cleaned"
    assert not workspace.exists()


def test_a_worktree_mode_reservation_still_cleans_through_git_worktree_remove(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    workspace = tmp_path / "workspaces" / "run-1"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    ticked = runtime.tick(
        now=NOW,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(
            _request(
                repository=repository,
                base=base,
                workspace=workspace,
                command=(sys.executable, "-c", "pass"),
                mode=WORKSPACE_MODE_GIT_WORKTREE,
            ),
        ),
    )
    _await_exit(ticked.dispatched[0])
    assert (workspace / ".git").is_file(), "a linked worktree has a .git file, not a directory"
    assert str(workspace) in _git("worktree", "list", cwd=repository)
    sentinel = out_path_for(workspace)
    sentinel.mkdir()

    cleaned = _backend(ledger_service, runtime).cleanup(
        ticked.dispatched[0].workspace, epoch=_epoch(runtime), now=NOW + timedelta(seconds=5)
    )

    assert cleaned.status == "cleaned"
    assert cleaned.mode == WORKSPACE_MODE_GIT_WORKTREE
    assert not workspace.exists()
    assert str(workspace) not in _git("worktree", "list", cwd=repository)
    # Only the task-local mode owns sibling out/intake directories; the worktree path leaves them.
    assert sentinel.is_dir()


# --- base revision resolved once ----------------------------------------------------------


def test_a_symbolic_base_is_resolved_once_and_verification_survives_the_name_moving(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, oid = _repository(tmp_path)
    _git("branch", "-M", "main", cwd=repository)
    workspace = tmp_path / "workspaces" / "run-1"
    runtime, dispatched = _dispatch_clone(
        ledger_service,
        repository=repository,
        base="main",
        workspace=workspace,
        command=_worker_command(oid, tmp_path / "marker", "implement"),
    )
    _await_exit(dispatched)
    # The symbolic name moves after the run started: verification must still bind to the base
    # the worker was given, not to wherever "main" points by now.
    (repository / "README").write_text("main moved on\n", encoding="utf-8")
    _git("commit", "-q", "-am", "main moves", cwd=repository)
    assert _git("rev-parse", "main", cwd=repository) != oid

    reservation = _backend(ledger_service, runtime).read_reservation(REPOSITORY_ID)
    assert reservation is not None
    assert reservation.base_revision == "main"
    assert reservation.base_oid == oid
    assert reservation.resolved_base_revision == oid
    stored = ledger_service.read_process_manager_state(
        process_manager_name="workspace-reservations", state_key=REPOSITORY_ID
    )
    assert stored is not None
    assert stored.state["base_revision"] == "main"
    assert stored.state["base_oid"] == oid

    head = runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)

    assert (
        _git("--git-dir", str(intake_path_for(workspace)), "rev-parse", f"{head}^", cwd=tmp_path)
        == oid
    )


def test_a_full_object_id_base_keeps_the_legacy_stored_shape(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, oid = _repository(tmp_path)
    runtime, dispatched = _dispatch_clone(
        ledger_service, repository=repository, base=oid, workspace=tmp_path / "workspaces" / "w"
    )
    _await_exit(dispatched)

    stored = ledger_service.read_process_manager_state(
        process_manager_name="workspace-reservations", state_key=REPOSITORY_ID
    )
    reservation = _backend(ledger_service, runtime).read_reservation(REPOSITORY_ID)

    assert stored is not None and reservation is not None
    assert "base_oid" not in stored.state
    assert reservation.base_oid is None
    assert reservation.resolved_base_revision == oid


def test_a_reservation_persisted_without_a_resolved_base_decodes_unchanged(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ledger_service.append(
        AppendCommand(
            correlation_id="legacy-base",
            events=(
                EventWrite(
                    "workspace",
                    "legacy-base-repository",
                    0,
                    "workspace.materialized",
                    1,
                    {"repository_id": "legacy-base-repository"},
                ),
            ),
            process_manager_updates=(
                ProcessManagerStateWrite(
                    process_manager_name="workspace-reservations",
                    state_key="legacy-base-repository",
                    expected_version=0,
                    state={
                        "repository_id": "legacy-base-repository",
                        "run_id": "legacy-run",
                        "repository_path": str(tmp_path / "repository"),
                        "workspace_path": str(tmp_path / "workspace"),
                        "base_revision": "main",
                        "status": "materialized",
                    },
                ),
            ),
        )
    )
    coordinator = Coordinator(ledger_service, owner="coordinator")

    reservation = GitWorktreeBackend(ledger_service, coordinator).read_reservation(
        "legacy-base-repository"
    )

    assert reservation is not None
    assert reservation.base_oid is None
    assert reservation.resolved_base_revision == "main"


def test_a_worktree_reservation_records_the_resolved_base_when_it_materializes(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, oid = _repository(tmp_path)
    _git("branch", "-M", "main", cwd=repository)
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=NOW, heartbeat_window=timedelta(seconds=60))
    backend = GitWorktreeBackend(ledger_service, coordinator)
    reserved = backend.reserve(
        repository_id=REPOSITORY_ID,
        run_id="run-1",
        repository_path=repository,
        workspace_path=tmp_path / "worktree",
        base_revision="main",
        epoch=epoch.epoch,
        now=NOW,
    )
    assert reserved.base_oid is None

    materialized = backend.materialize(reserved, epoch=epoch.epoch, now=NOW)
    (repository / "README").write_text("main moved on\n", encoding="utf-8")
    _git("commit", "-q", "-am", "main moves", cwd=repository)

    assert materialized.base_oid == oid
    assert backend.read_reservation(REPOSITORY_ID) == materialized
    assert _git("rev-parse", "HEAD", cwd=materialized.workspace_path) == oid
