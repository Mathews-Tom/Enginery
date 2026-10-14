from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.engine import task_repository
from enginery.engine.task_repository import (
    BUNDLE_FILE_NAME,
    intake_branch_bundle,
    intake_path_for,
    materialize_task_local_repository,
    out_path_for,
    remove_stale_base_references,
    remove_task_local_workspace,
    resolve_commit,
    verify_intake_branch,
)

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def _git(*args: str, cwd: Path, check: bool = True, stdin: str | None = None) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        **_IDENTITY,
    }
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        input=stdin,
    )
    return completed.stdout.strip()


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _make_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _worker_commit(
    workspace: Path, branch: str, *, base: str, files: dict[str, str] | None = None
) -> str:
    """Do what the worker does: commit on a branch and bundle ``BASE..BRANCH``."""
    _git("checkout", "-q", "-b", branch, cwd=workspace)
    for name, content in (files or {"feature.txt": "implemented\n"}).items():
        (workspace / name).write_text(content, encoding="utf-8")
        _git("add", name, cwd=workspace)
    _git("commit", "-q", "-m", "implement", cwd=workspace)
    out = out_path_for(workspace)
    out.mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(out / BUNDLE_FILE_NAME), f"{base}..{branch}", cwd=workspace)
    return _git("rev-parse", "HEAD", cwd=workspace)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "host" / "repo"
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _commit_file(path, "README", "first\n", "first")
    _commit_file(path, "README", "second\n", "second")
    return path


@pytest.fixture
def base(repository: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repository)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "run" / "workspace-1"


@pytest.fixture
def materialized(repository: Path, base: str, workspace: Path) -> Path:
    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )
    return workspace


def _bundle(workspace: Path) -> Path:
    return out_path_for(workspace) / BUNDLE_FILE_NAME


def _intake(repository: Path, workspace: Path, base: str, branch: str = "feature") -> str:
    return intake_branch_bundle(
        repository_path=repository,
        workspace_path=workspace,
        bundle_path=_bundle(workspace),
        head_branch=branch,
        base_revision=base,
    )


# --- paths ----------------------------------------------------------------------


def test_sibling_paths_are_derived_from_workspace_name(workspace: Path) -> None:
    assert out_path_for(workspace) == workspace.parent / "workspace-1.out"
    assert intake_path_for(workspace) == workspace.parent / "workspace-1.intake.git"


def test_resolve_commit_resolves_symbolic_revisions_and_rejects_unknown(
    repository: Path, base: str
) -> None:
    assert resolve_commit(repository, "HEAD") == base
    assert resolve_commit(repository, "main~1") != base
    with pytest.raises(ExternalConflictError):
        resolve_commit(repository, "does-not-exist")
    with pytest.raises(InvalidInputError):
        resolve_commit(repository, "--upload-pack=evil")


# --- materialization ------------------------------------------------------------


def test_clone_is_self_contained_detached_and_configured(
    repository: Path, base: str, materialized: Path
) -> None:
    git_dir = materialized / ".git"
    assert git_dir.is_dir() and not git_dir.is_symlink()
    assert not (git_dir / "objects" / "info" / "alternates").exists()
    assert _git("rev-parse", "HEAD", cwd=materialized) == base
    assert _git("symbolic-ref", "--quiet", "HEAD", cwd=materialized, check=False) == ""
    assert _git("remote", cwd=materialized) == ""
    assert (materialized / "README").read_text(encoding="utf-8") == "second\n"
    config = {
        key: _git("config", "--local", "--get", key, cwd=materialized)
        for key in (
            "user.name",
            "user.email",
            "commit.gpgsign",
            "gc.auto",
            "core.hooksPath",
        )
    }
    assert config == {
        "user.name": "Enginery Worker",
        "user.email": "worker@enginery.invalid",
        "commit.gpgsign": "false",
        "gc.auto": "0",
        "core.hooksPath": "/nonexistent-enginery-hooks",
    }
    host = str(repository).encode()
    for path in git_dir.rglob("*"):
        if path.is_file() and "objects" not in path.relative_to(git_dir).parts[:1]:
            assert host not in path.read_bytes(), path
    # Objects are copied, not shared: the clone survives deleting the source.
    shutil.rmtree(repository)
    assert _git("fsck", "--connectivity-only", cwd=materialized) == ""
    assert _git("log", "--format=%s", cwd=materialized).splitlines() == ["second", "first"]


def test_materialization_runs_no_source_hook_or_source_configured_command(
    repository: Path, base: str, workspace: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    hooks = tmp_path / "evil-hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-merge", "reference-transaction"):
        _make_executable(hooks / name, f'echo {name} >> "{marker}"')
        _make_executable(repository / ".git" / "hooks" / name, f'echo src-{name} >> "{marker}"')
    fsmonitor = tmp_path / "fsmonitor"
    _make_executable(fsmonitor, f'echo fsmonitor >> "{marker}"')
    _git("config", "core.hooksPath", str(hooks), cwd=repository)
    _git("config", "core.fsmonitor", str(fsmonitor), cwd=repository)
    _git("config", "core.sshCommand", f"touch {marker}; false", cwd=repository)

    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )

    assert not marker.exists()
    hooks_dir = workspace / ".git" / "hooks"
    assert not hooks_dir.exists() or not any(hooks_dir.iterdir())
    assert (workspace / "README").read_text(encoding="utf-8") == "second\n"
    assert (
        _git(
            "config",
            "--local",
            "--get-regexp",
            "^core\\.(fsmonitor|sshcommand)$",
            cwd=workspace,
            check=False,
        )
        == ""
    )


def test_source_that_borrows_objects_yields_a_clone_that_does_not(
    repository: Path, base: str, workspace: Path, tmp_path: Path
) -> None:
    borrowing = tmp_path / "borrowing"
    _git("clone", "-q", "--shared", "--no-checkout", str(repository), str(borrowing), cwd=tmp_path)
    assert (borrowing / ".git" / "objects" / "info" / "alternates").exists()

    materialize_task_local_repository(
        repository_path=borrowing, workspace_path=workspace, base_revision=base
    )

    assert not (workspace / ".git" / "objects" / "info" / "alternates").exists()
    shutil.rmtree(borrowing)
    shutil.rmtree(repository)
    assert _git("fsck", "--connectivity-only", cwd=workspace) == ""
    assert _git("rev-parse", "HEAD", cwd=workspace) == base


def test_symbolic_base_revision_is_resolved_against_the_source(
    repository: Path, base: str, workspace: Path
) -> None:
    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision="main"
    )
    assert _git("rev-parse", "HEAD", cwd=workspace) == base


def test_materialization_accepts_an_empty_directory_only(
    repository: Path, base: str, workspace: Path
) -> None:
    workspace.parent.mkdir(mode=0o700)
    workspace.mkdir()
    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )
    assert (workspace / ".git").is_dir()

    with pytest.raises(ExternalConflictError, match="not empty"):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=workspace, base_revision=base
        )


def test_materialization_refuses_symlinked_and_file_workspaces(
    repository: Path, base: str, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ExternalConflictError, match="symbolic link"):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=link, base_revision=base
        )
    assert list(target.iterdir()) == []

    occupied = tmp_path / "occupied"
    occupied.write_text("x", encoding="utf-8")
    with pytest.raises(ExternalConflictError):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=occupied, base_revision=base
        )


def test_materialization_rejects_unknown_base_and_missing_repository(
    repository: Path, workspace: Path, tmp_path: Path
) -> None:
    with pytest.raises(ExternalConflictError):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=workspace, base_revision="0" * 40
        )
    assert not workspace.exists()
    with pytest.raises(InvalidInputError):
        materialize_task_local_repository(
            repository_path=tmp_path / "absent", workspace_path=workspace, base_revision="HEAD"
        )


# --- intake: round trip ---------------------------------------------------------


def test_worker_branch_round_trips_through_intake_and_verification(
    repository: Path, base: str, materialized: Path
) -> None:
    worker_head = _worker_commit(materialized, "feature", base=base)

    fetched = _intake(repository, materialized, base)
    verified = verify_intake_branch(
        intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
    )

    assert fetched == verified == worker_head
    intake = intake_path_for(materialized)
    alternates = (intake / "objects" / "info" / "alternates").read_text(encoding="utf-8")
    assert Path(alternates.strip()) == (repository / ".git" / "objects").resolve()
    assert _git("config", "--local", "--get", "core.hooksPath", cwd=intake) == "/dev/null"
    assert _git("--git-dir", str(intake), "show", f"{worker_head}:feature.txt", cwd=intake) == (
        "implemented"
    )
    # The host repository itself was never touched by the intake.
    assert _git("for-each-ref", "--format=%(refname)", cwd=repository) == "refs/heads/main"


def test_intake_is_idempotent_and_follows_a_replaced_bundle(
    repository: Path, base: str, materialized: Path
) -> None:
    first = _worker_commit(materialized, "feature", base=base)
    assert _intake(repository, materialized, base) == first
    assert _intake(repository, materialized, base) == first

    (materialized / "more.txt").write_text("more\n", encoding="utf-8")
    _git("add", "more.txt", cwd=materialized)
    _git("commit", "-q", "-m", "more", cwd=materialized)
    _bundle(materialized).unlink()
    _git("bundle", "create", "-q", str(_bundle(materialized)), f"{base}..feature", cwd=materialized)
    second = _git("rev-parse", "HEAD", cwd=materialized)

    assert _intake(repository, materialized, base) == second != first
    assert (
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )
        == second
    )


def test_existing_intake_that_belongs_to_another_repository_is_refused(
    repository: Path, base: str, materialized: Path, tmp_path: Path
) -> None:
    _worker_commit(materialized, "feature", base=base)
    _intake(repository, materialized, base)
    alternates = intake_path_for(materialized) / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path / "other" / "objects") + "\n", encoding="utf-8")

    with pytest.raises(ExternalConflictError, match="does not belong"):
        _intake(repository, materialized, base)


# --- intake: planted worker state is never executed -----------------------------


def test_worker_planted_hooks_and_config_are_never_executed(
    repository: Path, base: str, materialized: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "planted-marker"
    _worker_commit(materialized, "feature", base=base)
    git_dir = materialized / ".git"
    (git_dir / "hooks").mkdir(exist_ok=True)
    for name in ("pre-push", "post-checkout", "reference-transaction", "pre-auto-gc"):
        _make_executable(git_dir / "hooks" / name, f'echo {name} >> "{marker}"')
    monitor = tmp_path / "monitor"
    _make_executable(monitor, f'echo fsmonitor >> "{marker}"')
    with (git_dir / "config").open("a", encoding="utf-8") as config:
        config.write(
            "[core]\n"
            f"\tsshCommand = touch {marker}; false\n"
            f"\tfsmonitor = {monitor}\n"
            f"\thooksPath = {tmp_path / 'hooks-from-worker'}\n"
            "\tpager = false\n"
            "[uploadpack]\n"
            f"\tpackObjectsHook = {monitor}\n"
        )
    (tmp_path / "hooks-from-worker").mkdir()
    _make_executable(
        tmp_path / "hooks-from-worker" / "reference-transaction",
        f'echo hooks-from-worker >> "{marker}"',
    )

    head = _intake(repository, materialized, base)
    verify_intake_branch(
        intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
    )
    remove_task_local_workspace(workspace_path=materialized)

    assert head
    assert not marker.exists()


# --- intake: hostile bundles ----------------------------------------------------


def test_refs_other_than_the_requested_branch_are_ignored(
    repository: Path, base: str, materialized: Path
) -> None:
    _worker_commit(materialized, "feature", base=base)
    _git("branch", "other", "feature", cwd=materialized)
    _git("tag", "planted", "feature", cwd=materialized)
    _git(
        "bundle",
        "create",
        "-q",
        str(_bundle(materialized)),
        f"{base}..feature",
        f"{base}..other",
        "--tags",
        cwd=materialized,
    )

    _intake(repository, materialized, base)

    refs = _git("for-each-ref", "--format=%(refname)", cwd=intake_path_for(materialized))
    assert refs == "refs/heads/feature"
    assert _git("for-each-ref", "--format=%(refname)", cwd=repository) == "refs/heads/main"


def test_bundle_without_the_requested_branch_is_rejected(
    repository: Path, base: str, materialized: Path
) -> None:
    _worker_commit(materialized, "other", base=base)
    with pytest.raises(ExternalConflictError, match="intake failed"):
        _intake(repository, materialized, base, branch="feature")


def test_branch_that_does_not_descend_from_base_fails_verification(
    repository: Path, base: str, materialized: Path
) -> None:
    first = _git("rev-parse", "main~1", cwd=repository)
    _git("checkout", "-q", "-b", "feature", first, cwd=materialized)
    (materialized / "alt.txt").write_text("alt\n", encoding="utf-8")
    _git("add", "alt.txt", cwd=materialized)
    _git("commit", "-q", "-m", "alt", cwd=materialized)
    out = out_path_for(materialized)
    out.mkdir(exist_ok=True)
    _git(
        "bundle", "create", "-q", str(_bundle(materialized)), f"{first}..feature", cwd=materialized
    )

    _intake(repository, materialized, base)

    with pytest.raises(ExternalConflictError, match="does not descend"):
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )


def test_unrelated_history_bundle_fails_verification(
    repository: Path, base: str, materialized: Path
) -> None:
    _git("checkout", "-q", "--orphan", "feature", cwd=materialized)
    (materialized / "other.txt").write_text("other\n", encoding="utf-8")
    _git("add", "other.txt", cwd=materialized)
    _git("commit", "-q", "-m", "orphan", cwd=materialized)
    out_path_for(materialized).mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(_bundle(materialized)), "feature", cwd=materialized)

    _intake(repository, materialized, base)

    with pytest.raises(ExternalConflictError, match="does not descend"):
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )


def test_bundle_with_prerequisites_missing_from_the_host_is_rejected(
    repository: Path, base: str, materialized: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git("init", "-q", "-b", "feature", cwd=foreign)
    root = _commit_file(foreign, "a", "a\n", "a")
    _commit_file(foreign, "b", "b\n", "b")
    out_path_for(materialized).mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(_bundle(materialized)), f"{root}..feature", cwd=foreign)

    with pytest.raises(ExternalConflictError, match="verification failed"):
        _intake(repository, materialized, base)
    assert _git("for-each-ref", cwd=intake_path_for(materialized)) == ""


def test_bundle_without_any_change_fails_verification(
    repository: Path, base: str, materialized: Path
) -> None:
    _git("checkout", "-q", "-b", "feature", cwd=materialized)
    _git("commit", "-q", "--allow-empty", "-m", "nothing", cwd=materialized)
    out_path_for(materialized).mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(_bundle(materialized)), f"{base}..feature", cwd=materialized)
    _intake(repository, materialized, base)
    with pytest.raises(ExternalConflictError, match="no implementation change"):
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )


def test_branch_pointing_at_the_base_fails_verification(
    repository: Path, base: str, materialized: Path
) -> None:
    _git("branch", "feature", base, cwd=materialized)
    out_path_for(materialized).mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(_bundle(materialized)), "feature", cwd=materialized)
    _intake(repository, materialized, base)
    with pytest.raises(ExternalConflictError, match="no implementation change"):
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )


@pytest.mark.parametrize("mode", ["flip", "truncate", "garbage", "empty"])
def test_tampered_or_corrupt_bundle_is_rejected(
    repository: Path, base: str, materialized: Path, mode: str
) -> None:
    _worker_commit(materialized, "feature", base=base)
    bundle = _bundle(materialized)
    data = bytearray(bundle.read_bytes())
    if mode == "flip":
        for index in range(len(data) // 2, len(data)):
            data[index] ^= 0xFF
        bundle.write_bytes(bytes(data))
    elif mode == "truncate":
        bundle.write_bytes(bytes(data[: len(data) // 2]))
    elif mode == "garbage":
        bundle.write_bytes(b"not a bundle\n" * 64)
    else:
        bundle.write_bytes(b"")

    with pytest.raises(ExternalConflictError):
        _intake(repository, materialized, base)
    intake = intake_path_for(materialized)
    if intake.exists():  # an empty bundle is refused before any intake repository exists
        assert _git("for-each-ref", cwd=intake) == ""


def test_objects_that_fail_fsck_are_rejected(
    repository: Path, base: str, materialized: Path
) -> None:
    # A tree entry named ".git" is a classic repository-takeover payload.
    blob = _git("rev-parse", "HEAD:README", cwd=materialized)  # any existing blob
    raw = bytes.fromhex(blob)
    tree_bytes = b"100644 .git\x00" + raw
    tree = (
        subprocess.run(
            ["git", "hash-object", "-t", "tree", "-w", "--literally", "--stdin"],
            cwd=materialized,
            input=tree_bytes,
            capture_output=True,
            check=True,
            env={"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null"},
        )
        .stdout.decode()
        .strip()
    )
    commit = _git("commit-tree", tree, "-p", base, "-m", "takeover", cwd=materialized)
    _git("branch", "feature", commit, cwd=materialized)
    out_path_for(materialized).mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(_bundle(materialized)), f"{base}..feature", cwd=materialized)

    with pytest.raises(ExternalConflictError, match="intake failed") as failure:
        _intake(repository, materialized, base)
    assert "dotgit" in str(failure.value.details["stderr"]).lower().replace(".", "")
    assert _git("for-each-ref", cwd=intake_path_for(materialized)) == ""


# --- intake: input refusals -----------------------------------------------------


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "  ",
        "-flag",
        "a..b",
        "a@{1}",
        "x" * 300,
        "with\x01control",
        "with space",
        "refs/heads/feature",
        "HEAD",
        "ends.lock",
        "ends/",
        "a//b",
        "a:b",
        "a~1",
        ".hidden",
    ],
)
def test_non_plain_branch_names_are_refused(
    repository: Path, base: str, materialized: Path, branch: str
) -> None:
    _worker_commit(materialized, "feature", base=base)
    with pytest.raises(InvalidInputError):
        _intake(repository, materialized, base, branch=branch)
    with pytest.raises(InvalidInputError):
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch=branch, base_revision=base
        )
    assert not intake_path_for(materialized).exists()


def test_slashed_branch_names_are_accepted(repository: Path, base: str, materialized: Path) -> None:
    head = _worker_commit(materialized, "enginery/task-1", base=base)
    assert _intake(repository, materialized, base, branch="enginery/task-1") == head


def test_bundle_path_must_be_a_regular_file_within_the_size_limit(
    repository: Path,
    base: str,
    materialized: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _worker_commit(materialized, "feature", base=base)
    real = _bundle(materialized)

    link = tmp_path / "link.bundle"
    link.symlink_to(real)
    directory = tmp_path / "dir.bundle"
    directory.mkdir()
    for bad, message in (
        (link, "regular file"),
        (directory, "regular file"),
        (tmp_path / "absent.bundle", "missing"),
    ):
        with pytest.raises(ExternalConflictError, match=message):
            intake_branch_bundle(
                repository_path=repository,
                workspace_path=materialized,
                bundle_path=bad,
                head_branch="feature",
                base_revision=base,
            )

    monkeypatch.setattr(task_repository, "MAX_BUNDLE_BYTES", real.stat().st_size - 1)
    with pytest.raises(ExternalConflictError, match="size limit"):
        _intake(repository, materialized, base)
    assert not intake_path_for(materialized).exists()


def test_symlinked_workspace_is_refused_by_intake(
    repository: Path, base: str, materialized: Path, tmp_path: Path
) -> None:
    _worker_commit(materialized, "feature", base=base)
    link = tmp_path / "run" / "link-workspace"
    link.symlink_to(materialized)
    out_path_for(link).symlink_to(out_path_for(materialized))
    with pytest.raises(ExternalConflictError, match="symbolic link"):
        intake_branch_bundle(
            repository_path=repository,
            workspace_path=link,
            bundle_path=_bundle(materialized),
            head_branch="feature",
            base_revision=base,
        )
    assert not intake_path_for(link).exists()


def test_verify_requires_a_full_object_id_and_a_real_intake_directory(
    repository: Path, base: str, materialized: Path, tmp_path: Path
) -> None:
    _worker_commit(materialized, "feature", base=base)
    _intake(repository, materialized, base)
    intake = intake_path_for(materialized)
    with pytest.raises(InvalidInputError, match="full object id"):
        verify_intake_branch(intake_path=intake, head_branch="feature", base_revision="HEAD")
    with pytest.raises(ExternalConflictError, match="intake repository"):
        verify_intake_branch(
            intake_path=tmp_path / "absent.git", head_branch="feature", base_revision=base
        )
    with pytest.raises(ExternalConflictError, match="branch verification failed"):
        verify_intake_branch(intake_path=intake, head_branch="missing", base_revision=base)


# --- cleanup --------------------------------------------------------------------


def _populate_for_cleanup(workspace: Path, base: str, repository: Path) -> None:
    _worker_commit(workspace, "feature", base=base)
    _intake(repository, workspace, base)


def test_cleanup_removes_workspace_out_and_intake(
    repository: Path, base: str, materialized: Path
) -> None:
    _populate_for_cleanup(materialized, base, repository)

    retained = remove_task_local_workspace(workspace_path=materialized)

    assert retained == ()
    assert not materialized.exists()
    assert not out_path_for(materialized).exists()
    assert not intake_path_for(materialized).exists()
    assert (repository / ".git" / "objects").is_dir()


def test_cleanup_of_missing_paths_is_a_no_op(workspace: Path) -> None:
    assert remove_task_local_workspace(workspace_path=workspace) == ()


def test_cleanup_repairs_owner_permissions_before_removing(
    repository: Path, base: str, materialized: Path
) -> None:
    _populate_for_cleanup(materialized, base, repository)
    locked = materialized / "locked" / "inner"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x", encoding="utf-8")
    (locked / "file").chmod(0o000)
    locked.chmod(0o000)
    (materialized / "locked").chmod(0o500)
    out = out_path_for(materialized)
    (out / "readonly").write_text("x", encoding="utf-8")
    (out / "readonly").chmod(0o400)
    out.chmod(0o500)

    retained = remove_task_local_workspace(workspace_path=materialized)

    assert retained == ()
    assert not materialized.exists()
    assert not out.exists()


def test_cleanup_never_follows_symlinks_out_of_the_tree(materialized: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    outside.chmod(0o500)
    (materialized / "escape").symlink_to(outside)
    (materialized / "escape-file").symlink_to(outside / "keep.txt")
    out = out_path_for(materialized)
    out.mkdir(exist_ok=True)
    (out / "escape").symlink_to(outside)

    retained = remove_task_local_workspace(workspace_path=materialized)

    assert retained == ()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    outside.chmod(0o700)


def test_cleanup_of_a_symlinked_workspace_removes_only_the_link(
    materialized: Path, tmp_path: Path
) -> None:
    link = tmp_path / "run" / "link-workspace"
    link.symlink_to(materialized)

    assert remove_task_local_workspace(workspace_path=link) == ()

    assert not link.is_symlink()
    assert (materialized / ".git").is_dir()


def test_cleanup_reports_entries_that_cannot_be_removed_and_removes_the_rest(
    repository: Path,
    base: str,
    materialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate_for_cleanup(materialized, base, repository)
    stuck = materialized / "sub" / "foreign-owned"
    stuck.parent.mkdir()
    stuck.write_text("x", encoding="utf-8")
    real_unlink = os.unlink

    def refuse(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if Path(path) == stuck:
            raise PermissionError(1, "Operation not permitted", str(path))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", refuse)

    retained = remove_task_local_workspace(workspace_path=materialized)

    assert retained == (stuck,)
    assert stuck.exists()
    assert not (materialized / ".git").exists()
    assert not out_path_for(materialized).exists()
    assert not intake_path_for(materialized).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="immutable flags need chflags (macOS)")
def test_cleanup_retains_a_really_immutable_entry(materialized: Path) -> None:
    stuck = materialized / "sub" / "immutable"
    stuck.parent.mkdir()
    stuck.write_text("x", encoding="utf-8")
    flags = getattr(stat, "UF_IMMUTABLE")  # noqa: B009 - only defined on BSD/macOS
    os.chflags(stuck, flags)  # type: ignore[attr-defined,unused-ignore]
    try:
        retained = remove_task_local_workspace(workspace_path=materialized)
        assert retained == (stuck,)
        assert stuck.exists()
    finally:
        os.chflags(stuck, 0)  # type: ignore[attr-defined,unused-ignore]
        shutil.rmtree(materialized, ignore_errors=True)


# --- base-only clone (no host object leakage) ----------------------------------------------


def _object_ids(repository: Path) -> set[str]:
    listing = _git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)", cwd=repository)
    return set(listing.split())


def test_the_clone_holds_only_what_is_reachable_from_the_base(
    repository: Path, base: str, workspace: Path
) -> None:
    canary_blob = _git("hash-object", "-w", "--stdin", cwd=repository, stdin="unreachable canary\n")
    (repository / "README").write_text("stashed\n", encoding="utf-8")
    _git("stash", "push", "-q", cwd=repository)
    stash = _git("rev-parse", "refs/stash", cwd=repository)
    _git("checkout", "-q", "-b", "unrelated", cwd=repository)
    _commit_file(repository, "branch-only.txt", "branch canary\n", "unrelated branch")
    branch_commit = _git("rev-parse", "HEAD", cwd=repository)
    _git("tag", "canary-tag", cwd=repository)
    _git("tag", "-a", "-m", "annotated canary", "canary-annotated", cwd=repository)
    annotated = _git("rev-parse", "canary-annotated", cwd=repository)
    _git("checkout", "-q", "main", cwd=repository)
    canaries = {canary_blob, stash, branch_commit, annotated}
    assert canaries <= _object_ids(repository), "the canaries exist in the host repository"

    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )

    clone_objects = _object_ids(workspace)
    for canary in canaries:
        assert canary not in clone_objects, canary
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", canary],
                cwd=workspace,
                env={"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null"},
                capture_output=True,
                check=False,
            ).returncode
            != 0
        ), canary
    reachable = {
        entry
        for entry in _git("rev-list", "--objects", base, cwd=repository).split()
        if len(entry) in {40, 64}
    }
    assert clone_objects == reachable
    assert _git("for-each-ref", "--format=%(refname)", cwd=workspace) == "", (
        "the clone carries no branch, tag, stash or remote-tracking ref"
    )


def test_materialization_leaves_no_temporary_reference_in_the_host_repository(
    repository: Path, base: str, workspace: Path
) -> None:
    before = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repository)

    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )

    assert _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repository) == before


def test_a_failed_clone_still_removes_the_temporary_reference(
    repository: Path, base: str, tmp_path: Path
) -> None:
    before = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repository)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    occupied = parent / "workspace-1"
    occupied.write_text("a file blocks the clone target", encoding="utf-8")

    with pytest.raises(ExternalConflictError):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=occupied, base_revision=base
        )

    assert _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repository) == before


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _plant_base_ref(repository: Path, base: str, pid: int) -> str:
    name = f"enginery-base-{pid}-{'a' * 32}"
    _git("update-ref", f"refs/heads/{name}", base, cwd=repository)
    return name


def _branches(repository: Path) -> set[str]:
    return set(
        _git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=repository).split()
    )


def test_a_crashed_materializations_temporary_branch_is_swept_and_a_live_ones_is_not(
    repository: Path, base: str
) -> None:
    stale = _plant_base_ref(repository, base, _dead_pid())
    live = _plant_base_ref(repository, base, os.getpid())
    other = "enginery-base-notmine"
    _git("update-ref", f"refs/heads/{other}", base, cwd=repository)

    removed = remove_stale_base_references(repository)

    assert removed == (stale,)
    remaining = _branches(repository)
    assert stale not in remaining
    assert {live, other} <= remaining


def test_materialization_sweeps_the_leftover_of_a_crashed_one(
    repository: Path, base: str, workspace: Path
) -> None:
    stale = _plant_base_ref(repository, base, _dead_pid())

    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )

    assert stale not in _branches(repository)


# --- private parent directory -------------------------------------------------------------


def test_materialization_creates_the_parent_directory_operator_private(
    repository: Path, base: str, workspace: Path
) -> None:
    materialize_task_local_repository(
        repository_path=repository, workspace_path=workspace, base_revision=base
    )

    info = workspace.parent.lstat()
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.geteuid()


def test_a_laxer_existing_parent_is_refused_and_nothing_is_cloned(
    repository: Path, base: str, workspace: Path
) -> None:
    workspace.parent.mkdir()
    workspace.parent.chmod(0o755)

    with pytest.raises(ExternalConflictError, match="mode 0700"):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=workspace, base_revision=base
        )

    assert not workspace.exists()


def test_a_symlinked_parent_is_refused(
    repository: Path, base: str, workspace: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    workspace.parent.symlink_to(target)

    with pytest.raises(ExternalConflictError, match="mode 0700"):
        materialize_task_local_repository(
            repository_path=repository, workspace_path=workspace, base_revision=base
        )

    assert list(target.iterdir()) == []


# --- intake tree bounds (git bombs) -------------------------------------------------------


def _bundle_branch_with_tree(
    workspace: Path, base: str, tree: str, branch: str = "feature"
) -> None:
    commit = _git("commit-tree", "-p", base, "-m", "bomb", tree, cwd=workspace)
    _git("update-ref", f"refs/heads/{branch}", commit, cwd=workspace)
    out = out_path_for(workspace)
    out.mkdir(exist_ok=True)
    _git("bundle", "create", "-q", str(out / BUNDLE_FILE_NAME), f"{base}..{branch}", cwd=workspace)


def _flat_tree(workspace: Path, *, files: int, size: int) -> str:
    blob = _git("hash-object", "-w", "--stdin", cwd=workspace, stdin="x" * size)
    listing = "".join(f"100644 blob {blob}\tfile-{index:05d}\n" for index in range(files))
    return _git("mktree", cwd=workspace, stdin=listing)


def test_a_branch_tree_over_the_entry_count_is_refused_before_verification_passes(
    repository: Path, base: str, materialized: Path
) -> None:
    _bundle_branch_with_tree(materialized, base, _flat_tree(materialized, files=150, size=1))
    _intake(repository, materialized, base)
    intake = intake_path_for(materialized)

    with pytest.raises(ExternalConflictError, match="entry-count or size limit"):
        verify_intake_branch(
            intake_path=intake, head_branch="feature", base_revision=base, max_tree_entries=100
        )
    assert verify_intake_branch(
        intake_path=intake, head_branch="feature", base_revision=base, max_tree_entries=150
    ) == _git("rev-parse", "refs/heads/feature", cwd=intake)


def test_a_branch_tree_over_the_total_size_is_refused(
    repository: Path, base: str, materialized: Path
) -> None:
    _bundle_branch_with_tree(materialized, base, _flat_tree(materialized, files=10, size=1000))
    _intake(repository, materialized, base)

    with pytest.raises(ExternalConflictError, match="entry-count or size limit"):
        verify_intake_branch(
            intake_path=intake_path_for(materialized),
            head_branch="feature",
            base_revision=base,
            max_tree_bytes=9_999,
        )


def test_a_tree_that_expands_to_millions_of_entries_from_a_few_objects_is_stopped(
    repository: Path, base: str, materialized: Path
) -> None:
    """A real git bomb: 8 small objects whose recursive listing is 10**6 entries."""
    blob = _git("hash-object", "-w", "--stdin", cwd=materialized, stdin="x")
    tree = _git("mktree", cwd=materialized, stdin=f"100644 blob {blob}\tleaf\n")
    for _level in range(6):
        listing = "".join(f"040000 tree {tree}\td{index}\n" for index in range(10))
        tree = _git("mktree", cwd=materialized, stdin=listing)
    _bundle_branch_with_tree(materialized, base, tree)
    _intake(repository, materialized, base)

    started = time.monotonic()
    with pytest.raises(ExternalConflictError, match="entry-count or size limit"):
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )
    assert time.monotonic() - started < 120


def test_the_default_tree_bounds_accept_an_ordinary_change(
    repository: Path, base: str, materialized: Path
) -> None:
    head = _worker_commit(materialized, "feature", base=base)
    _intake(repository, materialized, base)

    assert (
        verify_intake_branch(
            intake_path=intake_path_for(materialized), head_branch="feature", base_revision=base
        )
        == head
    )
