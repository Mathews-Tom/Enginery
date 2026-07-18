from __future__ import annotations

import subprocess
from pathlib import Path

from enginery.engine.recovery import assess_workspace_quiescence


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout


def test_workspace_lock_blocks_automatic_recovery(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    lock_path = Path(_git("rev-parse", "--git-path", "index.lock", cwd=repository).strip())
    lock_path.touch()

    assessment = assess_workspace_quiescence(repository)

    assert not assessment.ready_to_release
    assert assessment.reason == "workspace_git_lock_present"
