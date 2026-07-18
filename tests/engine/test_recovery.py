from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from enginery.engine.recovery import assess_orphan, assess_workspace_quiescence
from enginery.ledger.process_manager import ProcessManagerStateRecord


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


def test_missing_process_identity_blocks_automatic_recovery(tmp_path: Path) -> None:
    record = ProcessManagerStateRecord(
        process_manager_name="worker-supervisor",
        state_key="run-1:node-1",
        state_version=1,
        state={"status": "launching"},
        updated_at=datetime.now(UTC).isoformat(),
    )

    assessment = assess_orphan(process_state=record, workspace_path=tmp_path)

    assert not assessment.ready_to_release
    assert assessment.reason == "supervisor_identity_missing_or_invalid"
