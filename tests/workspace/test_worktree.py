from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.workspace import GitWorktreeBackend, WorkspaceReservation
from enginery.ledger.service import LedgerService


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.invalid", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=path)
    _git("commit", "-m", "fixture", cwd=path)
    return path


def test_reservation_prevents_workspace_collision_and_cleans_worktree(
    ledger_service: LedgerService, repository: Path, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 14, 0, tzinfo=UTC)
    coordinator = Coordinator(ledger_service, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    backend = GitWorktreeBackend(ledger_service, coordinator)
    reservation = backend.reserve(
        repository_id="repo-1",
        run_id="run-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision="HEAD",
        epoch=epoch.epoch,
        now=now,
    )

    with pytest.raises(ExternalConflictError):
        backend.reserve(
            repository_id="repo-1",
            run_id="run-2",
            repository_path=repository,
            workspace_path=tmp_path / "workspace-2",
            base_revision="HEAD",
            epoch=epoch.epoch,
            now=now,
        )

    materialized = backend.materialize(reservation, epoch=epoch.epoch, now=now)
    assert (materialized.workspace_path / "README").is_file()
    cleaned = backend.cleanup(materialized, epoch=epoch.epoch, now=now)
    assert cleaned.status == "cleaned"
    assert not cleaned.workspace_path.exists()


def test_verifies_implemented_branch_is_current_and_pushed(
    ledger_service: LedgerService, repository: Path, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 14, 0, tzinfo=UTC)
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repository)
    base_revision = _git("rev-parse", "HEAD", cwd=repository)
    coordinator = Coordinator(ledger_service, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    backend = GitWorktreeBackend(ledger_service, coordinator)
    reservation = backend.reserve(
        repository_id="repo-1",
        run_id="run-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision=base_revision,
        epoch=epoch.epoch,
        now=now,
    )
    materialized = backend.materialize(reservation, epoch=epoch.epoch, now=now)
    _git("switch", "-c", "enginery/run-1", cwd=materialized.workspace_path)
    (materialized.workspace_path / "README").write_text("implemented\n", encoding="utf-8")
    _git("add", "README", cwd=materialized.workspace_path)
    _git("commit", "-m", "implement", cwd=materialized.workspace_path)
    with pytest.raises(ExternalConflictError, match="git branch verification failed"):
        backend.verify_implementation_branch(materialized, head_branch="enginery/run-1")
    _git("push", "--set-upstream", "origin", "enginery/run-1", cwd=materialized.workspace_path)

    assert backend.verify_implementation_branch(materialized, head_branch="enginery/run-1") == _git(
        "rev-parse", "HEAD", cwd=materialized.workspace_path
    )


def test_verify_implementation_branch_rejects_local_commit_ahead_of_origin(
    ledger_service: LedgerService, repository: Path, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repository)
    base_revision = _git("rev-parse", "HEAD", cwd=repository)
    coordinator = Coordinator(ledger_service, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    backend = GitWorktreeBackend(ledger_service, coordinator)
    reservation = backend.reserve(
        repository_id="repo-1",
        run_id="run-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision=base_revision,
        epoch=epoch.epoch,
        now=now,
    )
    materialized = backend.materialize(reservation, epoch=epoch.epoch, now=now)
    _git("switch", "-c", "enginery/run-1", cwd=materialized.workspace_path)
    (materialized.workspace_path / "README").write_text("implemented\n", encoding="utf-8")
    _git("add", "README", cwd=materialized.workspace_path)
    _git("commit", "-m", "implement", cwd=materialized.workspace_path)
    _git("push", "--set-upstream", "origin", "enginery/run-1", cwd=materialized.workspace_path)

    (materialized.workspace_path / "README").write_text("implemented again\n", encoding="utf-8")
    _git("add", "README", cwd=materialized.workspace_path)
    _git("commit", "-m", "implement further", cwd=materialized.workspace_path)

    with pytest.raises(
        ExternalConflictError, match="origin branch differs from the verified workspace revision"
    ):
        backend.verify_implementation_branch(materialized, head_branch="enginery/run-1")


def _materialized_worktree(
    ledger_service: LedgerService, repository: Path, tmp_path: Path
) -> tuple[GitWorktreeBackend, WorkspaceReservation]:
    now = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repository)
    coordinator = Coordinator(ledger_service, owner="coordinator-a")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    backend = GitWorktreeBackend(ledger_service, coordinator)
    reservation = backend.reserve(
        repository_id="repo-1",
        run_id="run-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision=_git("rev-parse", "HEAD", cwd=repository),
        epoch=epoch.epoch,
        now=now,
    )
    return backend, backend.materialize(reservation, epoch=epoch.epoch, now=now)


def test_an_operator_published_branch_is_verified_without_being_on_origin(
    ledger_service: LedgerService, repository: Path, tmp_path: Path
) -> None:
    backend, materialized = _materialized_worktree(ledger_service, repository, tmp_path)
    workspace = materialized.workspace_path
    _git("switch", "-c", "enginery/run-1", cwd=workspace)
    (workspace / "README").write_text("implemented\n", encoding="utf-8")
    _git("add", "README", cwd=workspace)
    _git("commit", "-m", "implement", cwd=workspace)

    # The worker-pushes contract still demands the branch on origin ...
    with pytest.raises(ExternalConflictError, match="git branch verification failed"):
        backend.verify_implementation_branch(materialized, head_branch="enginery/run-1")
    # ... while the operator-publishes contract verifies the local branch and leaves publishing
    # to the host, so nothing is required of the remote.
    assert backend.verify_implementation_branch(
        materialized, head_branch="enginery/run-1", require_remote=False
    ) == _git("rev-parse", "HEAD", cwd=workspace)


def test_skipping_the_remote_check_does_not_skip_the_local_branch_checks(
    ledger_service: LedgerService, repository: Path, tmp_path: Path
) -> None:
    backend, materialized = _materialized_worktree(ledger_service, repository, tmp_path)
    workspace = materialized.workspace_path

    _git("switch", "-c", "enginery/run-1", cwd=workspace)
    with pytest.raises(ExternalConflictError, match="contains no implementation change"):
        backend.verify_implementation_branch(
            materialized, head_branch="enginery/run-1", require_remote=False
        )
    with pytest.raises(ExternalConflictError, match="differs from the requested"):
        backend.verify_implementation_branch(
            materialized, head_branch="enginery/another", require_remote=False
        )
