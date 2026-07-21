"""Tests for enginery.incidents.hotfix."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from enginery.adapters.local import LocalValidation
from enginery.application.delivery_ports import ValidationStatus
from enginery.domain.errors import ExternalConflictError
from enginery.domain.ids import OperationId, RunId
from enginery.incidents.hotfix import (
    HotfixRepair,
    apply_repair,
    create_hotfix_worktree,
    emergency_pull_request_request,
    prove_non_vacuous_regression,
    remove_hotfix_worktree,
)

_RUN_ID = RunId("run-hotfix-1")
_BUGGY_APP = "def add(a, b):\n    return a + b + 1\n"
_FIXED_APP = "def add(a, b):\n    return a + b\n"
_CHECK_COMMAND = (
    "python3",
    "-c",
    "exec(open('app.py').read()); assert add(2, 3) == 5",
)


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
    (path / "app.py").write_text(_BUGGY_APP, encoding="utf-8")
    _git("add", "app.py", cwd=path)
    _git("commit", "-m", "v1: buggy add()", cwd=path)
    return path


class TestCreateHotfixWorktree:
    def test_creates_a_worktree_at_the_base_revision(
        self, repository: Path, tmp_path: Path
    ) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)

        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )

        assert workspace.root.is_dir()
        assert (workspace.root / "app.py").read_text(encoding="utf-8") == _BUGGY_APP
        assert workspace.branch == "hotfix/incident-1"

    def test_rejects_an_existing_worktree_root(self, repository: Path, tmp_path: Path) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        existing = tmp_path / "hotfix-workspace"
        existing.mkdir()

        with pytest.raises(ExternalConflictError, match="already exists"):
            create_hotfix_worktree(
                repository=repository,
                base_revision=base_revision,
                branch="hotfix/incident-1",
                worktree_root=existing,
            )


class TestApplyRepair:
    def test_writes_and_commits_the_repair(self, repository: Path, tmp_path: Path) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )
        repair = HotfixRepair(
            file_path="app.py", content=_FIXED_APP, commit_message="fix off-by-one in add()"
        )

        repaired_revision = apply_repair(workspace, repair)

        assert repaired_revision != base_revision
        assert (workspace.root / "app.py").read_text(encoding="utf-8") == _FIXED_APP


class TestProveNonVacuousRegression:
    def test_fails_unfixed_and_passes_repaired(self, repository: Path, tmp_path: Path) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )
        repair = HotfixRepair(
            file_path="app.py", content=_FIXED_APP, commit_message="fix off-by-one in add()"
        )
        repaired_revision = apply_repair(workspace, repair)
        validation = LocalValidation()

        evidence = prove_non_vacuous_regression(
            validation,
            run_id=_RUN_ID,
            workspace=workspace,
            command=_CHECK_COMMAND,
            repaired_revision=repaired_revision,
        )

        assert evidence.unfixed_result.status is ValidationStatus.FAILED
        assert evidence.repaired_result.status is ValidationStatus.PASSED
        assert evidence.is_non_vacuous

    def test_leaves_the_worktree_at_the_repaired_branch(
        self, repository: Path, tmp_path: Path
    ) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )
        repair = HotfixRepair(
            file_path="app.py", content=_FIXED_APP, commit_message="fix off-by-one in add()"
        )
        repaired_revision = apply_repair(workspace, repair)
        validation = LocalValidation()

        prove_non_vacuous_regression(
            validation,
            run_id=_RUN_ID,
            workspace=workspace,
            command=_CHECK_COMMAND,
            repaired_revision=repaired_revision,
        )

        assert (workspace.root / "app.py").read_text(encoding="utf-8") == _FIXED_APP
        current_branch = _git("branch", "--show-current", cwd=workspace.root)
        assert current_branch == "hotfix/incident-1"

    def test_a_vacuous_check_is_detected_as_non_distinguishing(
        self, repository: Path, tmp_path: Path
    ) -> None:
        """A check that always passes must not be reported as non-vacuous."""
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )
        repair = HotfixRepair(
            file_path="app.py", content=_FIXED_APP, commit_message="fix off-by-one in add()"
        )
        repaired_revision = apply_repair(workspace, repair)
        validation = LocalValidation()

        evidence = prove_non_vacuous_regression(
            validation,
            run_id=_RUN_ID,
            workspace=workspace,
            command=("python3", "-c", "pass"),
            repaired_revision=repaired_revision,
        )

        assert not evidence.is_non_vacuous


class TestRemoveHotfixWorktree:
    def test_removes_the_worktree_directory(self, repository: Path, tmp_path: Path) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )

        remove_hotfix_worktree(repository=repository, workspace=workspace)

        assert not workspace.root.exists()

    def test_branch_and_commits_survive_removal(self, repository: Path, tmp_path: Path) -> None:
        base_revision = _git("rev-parse", "HEAD", cwd=repository)
        workspace = create_hotfix_worktree(
            repository=repository,
            base_revision=base_revision,
            branch="hotfix/incident-1",
            worktree_root=tmp_path / "hotfix-workspace",
        )
        repair = HotfixRepair(
            file_path="app.py", content=_FIXED_APP, commit_message="fix off-by-one in add()"
        )
        repaired_revision = apply_repair(workspace, repair)

        remove_hotfix_worktree(repository=repository, workspace=workspace)

        branches = _git("branch", "--list", "hotfix/incident-1", cwd=repository)
        assert "hotfix/incident-1" in branches
        show = _git("show", f"{repaired_revision}:app.py", cwd=repository)
        assert show == _FIXED_APP.strip()


class TestEmergencyPullRequestRequest:
    def test_builds_a_conventionally_titled_request(self) -> None:
        request = emergency_pull_request_request(
            head_branch="hotfix/incident-1",
            base_branch="main",
            summary="checkout returns 500",
            operation_id=OperationId(value="a" * 64),
        )

        assert request.title == "hotfix: checkout returns 500"
        assert request.head_branch == "hotfix/incident-1"
        assert request.base_branch == "main"
        assert "Deployment and rollback are separately authorized" in request.body
