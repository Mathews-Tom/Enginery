"""Who publishes the branch decides whether verification needs it on the remote.

A worker that pushes its own branch has not finished until the branch is on ``origin``; an
operator-published branch cannot be on ``origin`` yet, so the remote is not consulted. The choice
is persisted on the implement node and read back by ``verify_implementation_branch``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.application.worker_environment import TRUSTED_LOCAL_WORKER_KEYS
from enginery.domain.errors import ExternalConflictError
from enginery.engine.runtime import (
    BRANCH_PUBLICATION_OPERATOR,
    BRANCH_PUBLICATION_WORKER,
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
BRANCH = "enginery/run-1"
_WORKER = (
    "import subprocess, sys, pathlib\n"
    "branch = sys.argv[1]\n"
    "subprocess.run(['git', 'switch', '-q', '-c', branch], check=True)\n"
    "pathlib.Path('feature.txt').write_text('implemented\\n')\n"
    "subprocess.run(['git', 'add', 'feature.txt'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'implement'], check=True)\n"
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
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
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _dispatch_worktree_worker(
    ledger: LedgerService,
    tmp_path: Path,
    *,
    branch_publication: str,
    environment_keys: tuple[str, ...] | None,
) -> tuple[CoordinatorRuntime, DispatchedFixture]:
    repository, base = _repository(tmp_path)
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Worker",
        "GIT_AUTHOR_EMAIL": "worker@example.invalid",
        "GIT_COMMITTER_NAME": "Worker",
        "GIT_COMMITTER_EMAIL": "worker@example.invalid",
    }
    runtime = CoordinatorRuntime(ledger, owner="coordinator")
    ticked = runtime.tick(
        now=NOW,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(
            FixtureDispatch(
                run_id="run-1",
                node_id="implement",
                attempt_id="attempt-1",
                repository_id="repository-1",
                repository_path=repository,
                workspace_path=tmp_path / "workspace-1",
                base_revision=base,
                command=(
                    "env",
                    *(f"{key}={value}" for key, value in environment.items()),
                    sys.executable,
                    "-c",
                    _WORKER,
                    BRANCH,
                ),
                expected_attempt_version=0,
                operation_id="operation-1",
                environment_keys=environment_keys,
                branch_publication=branch_publication,
            ),
        ),
    )
    (dispatched,) = ticked.dispatched
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            reaped, status = os.waitpid(dispatched.identity.pid, os.WNOHANG)
        except ChildProcessError:
            return runtime, dispatched  # already reaped by a later Popen: the worker has exited
        if reaped == dispatched.identity.pid:
            assert os.waitstatus_to_exitcode(status) == 0
            return runtime, dispatched
        time.sleep(0.02)
    raise AssertionError("the supervised worker did not exit")


def test_an_operator_published_branch_verifies_with_no_branch_on_origin(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runtime, dispatched = _dispatch_worktree_worker(
        ledger_service,
        tmp_path,
        branch_publication=BRANCH_PUBLICATION_OPERATOR,
        environment_keys=TRUSTED_LOCAL_WORKER_KEYS,
    )
    workspace = dispatched.workspace.workspace_path
    assert _git("branch", "--show-current", cwd=workspace) == BRANCH
    assert _git("ls-remote", "origin", f"refs/heads/{BRANCH}", cwd=workspace) == ""

    head = runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)

    assert head == _git("rev-parse", "HEAD", cwd=workspace)


def test_a_worker_pushed_branch_still_has_to_be_on_origin(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runtime, dispatched = _dispatch_worktree_worker(
        ledger_service,
        tmp_path,
        branch_publication=BRANCH_PUBLICATION_WORKER,
        environment_keys=None,
    )
    workspace = dispatched.workspace.workspace_path

    with pytest.raises(ExternalConflictError, match="git branch verification failed"):
        runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH)

    _git("push", "-q", "origin", f"{BRANCH}:refs/heads/{BRANCH}", cwd=workspace)
    assert runtime.verify_implementation_branch(run_id="run-1", head_branch=BRANCH) == _git(
        "rev-parse", "HEAD", cwd=workspace
    )


def test_the_publication_contract_survives_a_coordinator_replacement(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _dispatch_worktree_worker(
        ledger_service,
        tmp_path,
        branch_publication=BRANCH_PUBLICATION_OPERATOR,
        environment_keys=TRUSTED_LOCAL_WORKER_KEYS,
    )

    replacement = CoordinatorRuntime(ledger_service, owner="replacement-coordinator")
    request = replacement.read_node_request(run_id="run-1", node_id="implement")

    assert request.branch_publication == BRANCH_PUBLICATION_OPERATOR
    assert request.environment_keys == TRUSTED_LOCAL_WORKER_KEYS
