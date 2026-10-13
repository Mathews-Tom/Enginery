"""Task-local repositories and host-side bundle intake for isolated runs.

An isolated worker receives a self-contained clone of the repository: no linked
worktree, no shared objects, no remote, no host hooks and no host configuration.
The worker hands its result back as a git bundle. The host never runs git inside
the worker-writable workspace; it fetches the bundle into a host-owned bare
intake repository and verifies the branch there.

Every git invocation uses an argument list, an explicit working directory and a
sanitized environment (no ambient git configuration, no prompts).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

from enginery.domain.errors import ExternalConflictError, InvalidInputError

MAX_BUNDLE_BYTES = 1 << 30
"""Largest worker bundle the host accepts."""

BUNDLE_FILE_NAME = "branch.bundle"
"""Name of the worker's result bundle inside its out directory."""

GIT_TIMEOUT_SECONDS = 900.0

WORKER_USER_NAME = "Enginery Worker"
WORKER_USER_EMAIL = "worker@enginery.invalid"
WORKER_HOOKS_PATH = "/nonexistent-enginery-hooks"

_INTAKE_SUFFIX = ".intake.git"
_OUT_SUFFIX = ".out"
_MAX_BRANCH_LENGTH = 200
_FULL_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def intake_path_for(workspace_path: Path) -> Path:
    """Host-owned bare intake repository that belongs to one workspace."""
    return workspace_path.with_name(workspace_path.name + _INTAKE_SUFFIX)


def out_path_for(workspace_path: Path) -> Path:
    """Directory the worker writes its result artifacts into."""
    return workspace_path.with_name(workspace_path.name + _OUT_SUFFIX)


def resolve_commit(repository_path: Path, revision: str) -> str:
    """Resolve ``revision`` to a full commit object id inside ``repository_path``."""
    _require_revision(revision, "revision")
    return _git_checked(
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        cwd=repository_path,
        message="revision does not resolve to a commit",
        details={"revision": revision},
    )


def materialize_task_local_repository(
    *, repository_path: Path, workspace_path: Path, base_revision: str
) -> None:
    """Create a self-contained detached clone at ``base_revision`` for the worker."""
    _require_revision(base_revision, "base_revision")
    if not repository_path.is_dir():
        raise InvalidInputError(
            "repository path is not a directory", details={"repository_path": str(repository_path)}
        )
    _require_empty_target(workspace_path)
    expected = resolve_commit(repository_path, base_revision)

    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    _git_checked(
        [
            "clone",
            "--local",
            "--no-hardlinks",
            "--no-checkout",
            "--template=",
            "--",
            str(repository_path),
            str(workspace_path),
        ],
        cwd=workspace_path.parent,
        message="task-local repository clone failed",
        details={"repository_path": str(repository_path)},
    )
    git_dir = workspace_path / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ExternalConflictError(
            "task-local repository must own a real .git directory",
            details={"workspace_path": str(workspace_path)},
        )
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        # A source that borrows objects makes the clone borrow them too: make the
        # clone self-contained before the borrowed path can leak into the worker.
        _git_checked(
            ["repack", "-a", "-d", "-q"],
            cwd=workspace_path,
            message="task-local repository could not absorb borrowed objects",
            details={"workspace_path": str(workspace_path)},
        )
        alternates.unlink()
    _git_checked(
        ["-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", expected],
        cwd=workspace_path,
        message="task-local repository checkout failed",
        details={"base_revision": base_revision},
    )
    head = _git_checked(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        cwd=workspace_path,
        message="task-local repository head could not be resolved",
        details={"workspace_path": str(workspace_path)},
    )
    if head != expected:
        raise ExternalConflictError(
            "task-local repository head differs from the requested base revision",
            details={"expected_revision": expected, "actual_revision": head},
        )
    _git_checked(
        ["remote", "remove", "origin"],
        cwd=workspace_path,
        message="task-local repository remote removal failed",
        details={"workspace_path": str(workspace_path)},
    )
    for key, value in (
        ("user.name", WORKER_USER_NAME),
        ("user.email", WORKER_USER_EMAIL),
        ("commit.gpgsign", "false"),
        ("gc.auto", "0"),
        ("core.hooksPath", WORKER_HOOKS_PATH),
    ):
        _git_checked(
            ["config", "--local", key, value],
            cwd=workspace_path,
            message="task-local repository configuration failed",
            details={"key": key},
        )
    _remove_tree_quietly(git_dir / "logs")
    _assert_self_contained(
        repository_path=repository_path, git_dir=git_dir, workspace_path=workspace_path
    )
    out_path = out_path_for(workspace_path)
    if out_path.is_symlink():
        raise ExternalConflictError(
            "task-local result directory is a symlink",
            details={"out_path": str(out_path)},
        )
    out_path.mkdir(parents=True, exist_ok=True)


def intake_branch_bundle(
    *,
    repository_path: Path,
    workspace_path: Path,
    bundle_path: Path,
    head_branch: str,
    base_revision: str,
) -> str:
    """Fetch ``head_branch`` from a worker bundle into the host-owned intake repository.

    Returns the fetched head revision. Only the requested branch is imported; other
    bundle refs are ignored. Nothing is ever executed inside the worker workspace.
    """
    _require_branch_name(head_branch)
    _require_revision(base_revision, "base_revision")
    _require_regular_bundle(bundle_path)
    if workspace_path.is_symlink():
        raise ExternalConflictError(
            "workspace path must not be a symbolic link",
            details={"workspace_path": str(workspace_path)},
        )
    resolve_commit(repository_path, base_revision)
    intake = _ensure_intake_repository(
        repository_path=repository_path, intake_path=intake_path_for(workspace_path)
    )
    bundle = str(bundle_path.resolve())
    _git_checked(
        ["bundle", "verify", bundle],
        cwd=intake,
        git_dir=intake,
        message="worker bundle verification failed",
        details={"bundle_path": str(bundle_path)},
    )
    ref = f"refs/heads/{head_branch}"
    _git_checked(
        [
            "-c",
            "transfer.fsckObjects=true",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            bundle,
            f"+{ref}:{ref}",
        ],
        cwd=intake,
        git_dir=intake,
        message="worker bundle intake failed",
        details={"bundle_path": str(bundle_path), "head_branch": head_branch},
    )
    return _git_checked(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=intake,
        git_dir=intake,
        message="git branch verification failed",
        details={"head_branch": head_branch},
    )


def verify_intake_branch(*, intake_path: Path, head_branch: str, base_revision: str) -> str:
    """Verify an intake branch descends from its base and carries a change.

    ``base_revision`` must be a full object id (see :func:`resolve_commit`).
    Returns the verified head revision.
    """
    _require_branch_name(head_branch)
    if not _FULL_OBJECT_ID.fullmatch(base_revision):
        raise InvalidInputError(
            "base_revision must be a full object id", details={"base_revision": base_revision}
        )
    _require_intake_directory(intake_path)
    head_revision = _git_checked(
        ["rev-parse", "--verify", "--end-of-options", f"refs/heads/{head_branch}^{{commit}}"],
        cwd=intake_path,
        git_dir=intake_path,
        message="git branch verification failed",
        details={"head_branch": head_branch},
    )
    ancestor = _git(
        ["merge-base", "--is-ancestor", base_revision, head_revision],
        cwd=intake_path,
        git_dir=intake_path,
    )
    if ancestor.returncode != 0:
        raise ExternalConflictError(
            "workspace branch does not descend from its reserved base revision",
            details={
                "base_revision": base_revision,
                "head_revision": head_revision,
                "stderr": ancestor.stderr.strip(),
            },
        )
    changed = _git(
        ["diff", "--quiet", "--no-ext-diff", "--no-textconv", f"{base_revision}..{head_revision}"],
        cwd=intake_path,
        git_dir=intake_path,
    )
    if changed.returncode == 0:
        raise ExternalConflictError(
            "workspace branch contains no implementation change",
            details={"head_revision": head_revision},
        )
    if changed.returncode != 1:
        raise ExternalConflictError(
            "workspace change verification failed", details={"stderr": changed.stderr.strip()}
        )
    return head_revision


def remove_task_local_workspace(*, workspace_path: Path) -> tuple[Path, ...]:
    """Remove the workspace, its out directory and its intake repository.

    Permissions are repaired best-effort first (symbolic links are never followed).
    Entries that still cannot be removed are returned; this never raises for a
    partial failure so the caller can record what was retained.
    """
    retained: list[Path] = []
    for root in (workspace_path, out_path_for(workspace_path), intake_path_for(workspace_path)):
        _remove_root(root, retained)
    return tuple(retained)


# --- git plumbing -----------------------------------------------------------------


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(
    arguments: Sequence[str], *, cwd: Path, git_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    prefix = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
    if git_dir is not None:
        prefix.append(f"--git-dir={git_dir}")
    try:
        return subprocess.run(
            [*prefix, *arguments],
            cwd=cwd,
            env=_git_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExternalConflictError(
            "git could not be executed",
            details={"arguments": tuple(arguments), "error": str(error)},
        ) from error


def _git_checked(
    arguments: Sequence[str],
    *,
    cwd: Path,
    message: str,
    details: dict[str, object],
    git_dir: Path | None = None,
) -> str:
    completed = _git(arguments, cwd=cwd, git_dir=git_dir)
    if completed.returncode != 0:
        raise ExternalConflictError(
            message,
            details={
                **details,
                "arguments": tuple(arguments),
                "stderr": completed.stderr.strip(),
            },
        )
    return completed.stdout.strip()


# --- validation -------------------------------------------------------------------


def _require_revision(value: str, field_name: str) -> None:
    if not value.strip():
        raise InvalidInputError(f"{field_name} must be non-blank")
    if value.startswith("-") or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidInputError(f"{field_name} is not a plain revision", details={"value": value})


def _require_branch_name(head_branch: str) -> None:
    if not head_branch.strip():
        raise InvalidInputError("head_branch must be non-blank")
    problems = (
        len(head_branch) > _MAX_BRANCH_LENGTH,
        head_branch.startswith(("-", "/", ".")),
        head_branch.endswith(("/", ".", ".lock")),
        ".." in head_branch,
        "//" in head_branch,
        "@{" in head_branch,
        head_branch in {"@", "HEAD"},
        head_branch.startswith("refs/"),
        _BRANCH_FORBIDDEN.search(head_branch) is not None,
        any(part.startswith(".") or part.endswith(".lock") for part in head_branch.split("/")),
    )
    if any(problems):
        raise InvalidInputError(
            "head_branch is not a plain refs/heads branch name",
            details={"head_branch": head_branch[:_MAX_BRANCH_LENGTH]},
        )


def _require_empty_target(workspace_path: Path) -> None:
    if workspace_path.is_symlink():
        raise ExternalConflictError(
            "workspace path must not be a symbolic link",
            details={"workspace_path": str(workspace_path)},
        )
    if not workspace_path.exists():
        return
    if not workspace_path.is_dir() or any(workspace_path.iterdir()):
        raise ExternalConflictError(
            "workspace path already exists and is not empty",
            details={"workspace_path": str(workspace_path)},
        )


def _require_regular_bundle(bundle_path: Path) -> None:
    try:
        info = bundle_path.lstat()
    except OSError as error:
        raise ExternalConflictError(
            "worker bundle is missing", details={"bundle_path": str(bundle_path)}
        ) from error
    if not stat.S_ISREG(info.st_mode):
        raise ExternalConflictError(
            "worker bundle must be a regular file", details={"bundle_path": str(bundle_path)}
        )
    if info.st_size > MAX_BUNDLE_BYTES:
        raise ExternalConflictError(
            "worker bundle exceeds the size limit",
            details={"size": info.st_size, "limit": MAX_BUNDLE_BYTES},
        )
    if info.st_size == 0:
        raise ExternalConflictError(
            "worker bundle is empty", details={"bundle_path": str(bundle_path)}
        )


def _require_intake_directory(intake_path: Path) -> None:
    if intake_path.is_symlink() or not intake_path.is_dir():
        raise ExternalConflictError(
            "intake repository is missing or not a real directory",
            details={"intake_path": str(intake_path)},
        )


def _ensure_intake_repository(*, repository_path: Path, intake_path: Path) -> Path:
    objects = _git_checked(
        ["rev-parse", "--path-format=absolute", "--git-path", "objects"],
        cwd=repository_path,
        message="repository object directory could not be resolved",
        details={"repository_path": str(repository_path)},
    )
    alternates = intake_path / "objects" / "info" / "alternates"
    if intake_path.is_symlink():
        raise ExternalConflictError(
            "intake repository must not be a symbolic link",
            details={"intake_path": str(intake_path)},
        )
    if intake_path.exists():
        _require_intake_directory(intake_path)
        recorded = _read_text(alternates)
        if recorded.strip() != objects or not (intake_path / "HEAD").is_file():
            raise ExternalConflictError(
                "existing intake repository does not belong to this repository",
                details={"intake_path": str(intake_path)},
            )
        return intake_path
    intake_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git_checked(
            ["init", "--bare", "--quiet", "--template=", "--initial-branch=enginery-intake"],
            cwd=intake_path.parent,
            git_dir=intake_path,
            message="intake repository creation failed",
            details={"intake_path": str(intake_path)},
        )
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(objects + "\n", encoding="utf-8")
        for key, value in (
            ("core.hooksPath", "/dev/null"),
            ("gc.auto", "0"),
            ("transfer.fsckObjects", "true"),
            ("fetch.fsckObjects", "true"),
        ):
            _git_checked(
                ["config", "--local", key, value],
                cwd=intake_path,
                git_dir=intake_path,
                message="intake repository configuration failed",
                details={"key": key},
            )
    except BaseException:
        _remove_tree_quietly(intake_path)
        raise
    return intake_path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# --- self-containment assertions --------------------------------------------------


def _assert_self_contained(*, repository_path: Path, git_dir: Path, workspace_path: Path) -> None:
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise ExternalConflictError(
            "task-local repository must not borrow objects",
            details={"workspace_path": str(workspace_path)},
        )
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        executable = sorted(
            str(entry)
            for entry in hooks.rglob("*")
            if entry.is_file() and entry.stat().st_mode & 0o111
        )
        if executable:
            raise ExternalConflictError(
                "task-local repository carries executable hooks",
                details={"hooks": tuple(executable)},
            )
    needles = {str(repository_path).encode(), str(repository_path.resolve()).encode()}
    leaks: list[str] = []
    for entry in sorted(git_dir.rglob("*")):
        relative = entry.relative_to(git_dir)
        if relative.parts[0] in {"objects", "refs"} or not entry.is_file() or entry.is_symlink():
            continue
        content = entry.read_bytes()
        if any(needle in content for needle in needles):
            leaks.append(str(relative))
    if leaks:
        raise ExternalConflictError(
            "task-local repository references the host repository",
            details={"files": tuple(leaks)},
        )
    probe = _git(
        ["config", "--local", "--name-only", "--get-regexp", r"^(remote|branch)\."],
        cwd=workspace_path,
    )
    if probe.returncode == 0:
        raise ExternalConflictError(
            "task-local repository still carries remote configuration",
            details={"keys": tuple(probe.stdout.split())},
        )
    if probe.returncode != 1:
        raise ExternalConflictError(
            "task-local repository configuration could not be read",
            details={"stderr": probe.stderr.strip()},
        )


# --- removal ----------------------------------------------------------------------


def _remove_root(root: Path, retained: list[Path]) -> None:
    try:
        info = root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        retained.append(root)
        return
    if stat.S_ISDIR(info.st_mode):
        _remove_directory(root, retained)
        return
    try:
        root.unlink()
    except OSError:
        retained.append(root)


def _remove_directory(directory: Path, retained: list[Path]) -> bool:
    """Remove ``directory`` without following symlinks; True when it is gone."""
    _allow_owner_access(directory, directory=True)
    try:
        with os.scandir(directory) as scan:
            entries = list(scan)
    except OSError:
        retained.append(directory)
        return False
    clean = True
    for entry in entries:
        path = Path(entry.path)
        try:
            is_directory = entry.is_dir(follow_symlinks=False)
        except OSError:
            retained.append(path)
            clean = False
            continue
        if is_directory:
            clean = _remove_directory(path, retained) and clean
            continue
        _allow_owner_access(path, directory=False)
        try:
            os.unlink(path)
        except OSError:
            retained.append(path)
            clean = False
    if not clean:
        return False
    try:
        os.rmdir(directory)
    except OSError:
        retained.append(directory)
        return False
    return True


def _allow_owner_access(path: Path, *, directory: bool) -> None:
    """Best-effort ``chmod u+rwX`` that never follows a symbolic link."""
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return
        wanted = stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR
        if directory:
            wanted |= stat.S_IXUSR
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(path, wanted, follow_symlinks=False)
        else:
            os.chmod(path, wanted)
    except OSError:
        return


def _remove_tree_quietly(path: Path) -> None:
    """Remove a host-created tree during failure handling; leftovers are not fatal."""
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode):
        _remove_directory(path, [])
    else:
        try:
            path.unlink()
        except OSError:
            return


__all__ = [
    "BUNDLE_FILE_NAME",
    "GIT_TIMEOUT_SECONDS",
    "MAX_BUNDLE_BYTES",
    "WORKER_HOOKS_PATH",
    "WORKER_USER_EMAIL",
    "WORKER_USER_NAME",
    "intake_branch_bundle",
    "intake_path_for",
    "materialize_task_local_repository",
    "out_path_for",
    "remove_task_local_workspace",
    "resolve_commit",
    "verify_intake_branch",
]
