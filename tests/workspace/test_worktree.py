from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.workspace import GitWorktreeBackend
from enginery.ledger.service import LedgerService


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


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
