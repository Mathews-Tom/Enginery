"""Produce, with real git, what a collected isolated implementation leaves for the host.

An isolated worker commits on a branch inside its task-local clone and hands back a git bundle;
the host imports that bundle into an intake repository it owns and verifies the branch there.
These helpers play the worker and then run the host's own intake and verification, so a test can
start from a genuinely verified head revision rather than an invented one.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from enginery.engine.task_repository import (
    BUNDLE_FILE_NAME,
    intake_branch_bundle,
    intake_path_for,
    materialize_task_local_repository,
    out_path_for,
    verify_intake_branch,
)

_NO_GLOBAL_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(*args: str, cwd: Path) -> str:
    """Run git with no ambient configuration and return its stripped output."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_NO_GLOBAL_GIT},
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repository(path: Path, *, files: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """Create a repository with one baseline commit and return it with that commit's id."""
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "enginery@example.test", cwd=path)
    git("config", "user.name", "Enginery Test", cwd=path)
    for name, content in (files or {"tracked.txt": "baseline\n"}).items():
        (path / name).write_text(content, encoding="utf-8")
        git("add", name, cwd=path)
    git("commit", "-q", "-m", "baseline", cwd=path)
    return path, git("rev-parse", "HEAD", cwd=path)


def verified_head(
    *,
    repository: Path,
    base_revision: str,
    workspace_path: Path,
    head_branch: str,
    committed: Mapping[str, str],
    leftovers: Mapping[str, str] | None = None,
) -> str:
    """Materialize a worker workspace, commit ``committed`` on ``head_branch``, and verify it.

    ``leftovers`` are written to the workspace after the commit and never committed, the way a
    worker leaves scratch files behind. The returned revision is the host-verified head read from
    the host-owned intake repository, never from the worker's repository.
    """
    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace_path, base_revision=base_revision
    )
    git("checkout", "-q", "-b", head_branch, cwd=workspace_path)
    for name, content in committed.items():
        (workspace_path / name).write_text(content, encoding="utf-8")
        git("add", name, cwd=workspace_path)
    git("commit", "-q", "-m", "implement", cwd=workspace_path)
    bundle = out_path_for(workspace_path) / BUNDLE_FILE_NAME
    git("bundle", "create", str(bundle), f"{base_revision}..{head_branch}", cwd=workspace_path)
    for name, content in (leftovers or {}).items():
        (workspace_path / name).write_text(content, encoding="utf-8")
    intake_branch_bundle(
        repository_path=repository,
        workspace_path=workspace_path,
        bundle_path=bundle,
        head_branch=head_branch,
        base_revision=base_revision,
    )
    return verify_intake_branch(
        intake_path=intake_path_for(workspace_path),
        head_branch=head_branch,
        base_revision=base_revision,
    )
