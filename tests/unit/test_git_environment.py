"""Tests never write into an enclosing repository, whatever git exported to the suite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from enginery.adapters.local import LocalGit
from enginery.application.git_environment import (
    REPOSITORY_REDIRECT_VARIABLES,
    repository_isolated_environment,
    scrub_repository_variables,
)
from enginery.incidents.hotfix import _run_git

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**repository_isolated_environment(), **_ISOLATED, **(env or {})},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _decoy(tmp_path: Path) -> tuple[Path, str]:
    """A repository standing for the enclosing one, and a snapshot of its shared config."""
    decoy = tmp_path / "enclosing"
    decoy.mkdir()
    _git("init", "-q", "-b", "main", cwd=decoy)
    return decoy, (decoy / ".git" / "config").read_text(encoding="utf-8")


def test_the_suite_runs_with_no_repository_redirecting_variable() -> None:
    assert not (REPOSITORY_REDIRECT_VARIABLES & set(os.environ))


def test_a_copy_of_the_environment_drops_every_redirecting_variable() -> None:
    source = dict.fromkeys(REPOSITORY_REDIRECT_VARIABLES, "x") | {"PATH": "/usr/bin"}

    assert repository_isolated_environment(source) == {"PATH": "/usr/bin"}


def test_scrubbing_reports_and_removes_what_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/nowhere")
    monkeypatch.setenv("GIT_WORK_TREE", "/nowhere")

    assert scrub_repository_variables() == ("GIT_DIR", "GIT_WORK_TREE")
    assert "GIT_DIR" not in os.environ and "GIT_WORK_TREE" not in os.environ


def test_product_git_calls_stay_on_their_own_repository_when_git_dir_is_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy, before = _decoy(tmp_path)
    own = tmp_path / "own"
    own.mkdir()
    _git("init", "-q", "-b", "main", cwd=own)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    LocalGit(repository=own)._git("config", "user.name", "Own")
    _run_git(own, "config", "user.email", "own@example.invalid")

    assert (decoy / ".git" / "config").read_text(encoding="utf-8") == before
    own_config = (own / ".git" / "config").read_text(encoding="utf-8")
    assert "Own" in own_config and "own@example.invalid" in own_config


def test_a_suite_started_with_git_dir_set_runs_without_it_and_leaves_that_repository_alone(
    tmp_path: Path,
) -> None:
    """The real conftest scrub: a nested pytest that inherits GIT_DIR (as one started by
    `git rebase --exec` does) must see no redirecting variable and must not touch that repository.
    The nested run uses a representative fixture-style repository test of this project."""
    decoy, before = _decoy(tmp_path)
    environment = {
        **os.environ,
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        **_ISOLATED,
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/test_git_environment.py::test_the_suite_runs_with_no_repository_redirecting_variable",
            "tests/workspace/test_worktree.py",
        ],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout[-800:]
    assert (decoy / ".git" / "config").read_text(encoding="utf-8") == before
