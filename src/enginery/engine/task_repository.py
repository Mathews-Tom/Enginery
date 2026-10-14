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
import tempfile
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path

from enginery.domain.errors import ExternalConflictError, InvalidInputError

MAX_BUNDLE_BYTES = 1 << 30
"""Largest worker bundle the host accepts."""

MAX_TREE_ENTRIES = 200_000
"""Most entries (files, links, submodules) the host accepts in a worker branch's tree."""

MAX_TREE_BYTES = 2 << 30
"""Largest total blob size the host accepts in a worker branch's tree."""

BUNDLE_FILE_NAME = "branch.bundle"
"""Name of the worker's result bundle inside its out directory."""

GIT_TIMEOUT_SECONDS = 900.0

WORKER_USER_NAME = "Enginery Worker"
WORKER_USER_EMAIL = "worker@enginery.invalid"
WORKER_HOOKS_PATH = "/nonexistent-enginery-hooks"

_INTAKE_SUFFIX = ".intake.git"
_OUT_SUFFIX = ".out"
_MAX_BRANCH_LENGTH = 200
_TREE_READ_CHUNK = 1 << 16
_TREE_MAX_RECORD_BYTES = 1 << 20
_BASE_BRANCH_PREFIX = "enginery-base-"
_BASE_BRANCH = re.compile(r"^enginery-base-(?P<pid>[0-9]+)-[0-9a-f]{32}$")
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


def remove_stale_base_references(repository_path: Path) -> tuple[str, ...]:
    """Delete temporary base branches a crashed materialization left in the host repository.

    A materialization publishes its base under ``enginery-base-<pid>-<uuid>`` only while the
    clone runs. A branch whose creating process is gone can never be removed by that process,
    so it is removed here; one whose process is alive belongs to a live materialization and is
    left alone. Returns the removed branch names.
    """
    listed = _git(
        ["for-each-ref", "--format=%(refname:strip=2)", f"refs/heads/{_BASE_BRANCH_PREFIX}*"],
        cwd=repository_path,
    )
    if listed.returncode != 0:
        return ()
    removed: list[str] = []
    for name in listed.stdout.split():
        match = _BASE_BRANCH.fullmatch(name)
        if match is None or _process_alive(int(match["pid"])):
            continue
        if _git(["update-ref", "-d", f"refs/heads/{name}"], cwd=repository_path).returncode == 0:
            removed.append(name)
    return tuple(removed)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    remove_stale_base_references(repository_path)
    expected = resolve_commit(repository_path, base_revision)

    _ensure_private_parent(workspace_path.parent)
    base_branch = _clone_base_only(
        repository_path=repository_path, workspace_path=workspace_path, base_oid=expected
    )
    git_dir = workspace_path / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ExternalConflictError(
            "task-local repository must own a real .git directory",
            details={"workspace_path": str(workspace_path)},
        )
    _git_checked(
        ["-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", expected],
        cwd=workspace_path,
        message="task-local repository checkout failed",
        details={"base_revision": base_revision},
    )
    _git_checked(
        ["update-ref", "-d", f"refs/heads/{base_branch}"],
        cwd=workspace_path,
        message="task-local repository temporary branch removal failed",
        details={"workspace_path": str(workspace_path)},
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


def verify_intake_branch(
    *,
    intake_path: Path,
    head_branch: str,
    base_revision: str,
    max_tree_entries: int = MAX_TREE_ENTRIES,
    max_tree_bytes: int = MAX_TREE_BYTES,
) -> str:
    """Verify an intake branch descends from its base and carries a change.

    ``base_revision`` must be a full object id (see :func:`resolve_commit`). The head's
    recursive tree must stay within the entry-count and total-size bounds before it is
    accepted. Returns the verified head revision.
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
    _bound_tree(intake_path, head_revision, max_entries=max_tree_entries, max_bytes=max_tree_bytes)
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


_WORKTREE_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TMPDIR",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "XDG_CONFIG_HOME",
    }
)


def worktree_git_environment() -> dict[str, str]:
    """Environment for host git inside a linked worktree: an allowlist, never the ambient env.

    User and system git configuration stay readable (a trusted-local ``origin`` may need the
    operator's credential helper), but no ``GIT_*`` override, token or other ambient variable
    reaches git or anything it spawns.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _WORKTREE_ENVIRONMENT_KEYS or key.startswith("LC_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def run_worktree_git(
    arguments: Sequence[str], *, cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run host git in a linked worktree with fsmonitor and hooks disabled.

    A worktree's configuration is shared and editable by whoever worked in it, so
    ``core.fsmonitor`` and ``core.hooksPath`` are overridden on the command line and the
    environment is sanitized. A task-local clone is never a valid ``cwd`` here: the host runs
    no git at all inside one.
    """
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *arguments],
            cwd=cwd,
            env=worktree_git_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExternalConflictError(
            "git could not be executed",
            details={"arguments": tuple(arguments), "error": str(error)},
        ) from error


def _ensure_private_parent(parent: Path) -> None:
    """Require the directory holding workspace, result and intake to be operator-private.

    The worker-facing directories live side by side in this parent; nobody but the operator
    may list, replace or traverse it. A parent that already exists must already be a real
    directory owned by this user with no group/other permission bits; a laxer one is refused
    rather than silently re-permissioned, because it may be a shared directory.
    """
    parent.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        created = False
    else:
        created = True
        os.chmod(parent, 0o700)  # the creation mode is filtered through the umask
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ExternalConflictError(
            "task-local workspace parent must be a real directory owned by the operator with "
            "mode 0700",
            details={
                "parent": str(parent),
                "created": created,
                "mode": oct(stat.S_IMODE(info.st_mode)),
            },
        )


def _clone_base_only(*, repository_path: Path, workspace_path: Path, base_oid: str) -> str:
    """Clone only what is reachable from ``base_oid``; return the temporary branch name.

    The base is published under a unique temporary branch so a ``--no-local --single-branch
    --no-tags`` clone transfers exactly its reachable objects: unreachable blobs, stashes,
    other branches and tags of the host repository never reach the workspace. The temporary
    branch is removed from the host repository again before this returns. The branch name
    carries the creating process id so :func:`remove_stale_base_references` can tell the
    leftover of a crashed materialization from one that is still running.
    """
    branch = f"{_BASE_BRANCH_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    ref = f"refs/heads/{branch}"
    _git_checked(
        ["update-ref", ref, base_oid],
        cwd=repository_path,
        message="temporary base reference could not be created",
        details={"repository_path": str(repository_path)},
    )
    try:
        _git_checked(
            [
                "clone",
                "--no-local",
                "--single-branch",
                "--no-tags",
                "--no-checkout",
                "--template=",
                "--branch",
                branch,
                "--",
                str(repository_path),
                str(workspace_path),
            ],
            cwd=workspace_path.parent,
            message="task-local repository clone failed",
            details={"repository_path": str(repository_path)},
        )
    finally:
        removal = _git(["update-ref", "-d", ref], cwd=repository_path)
    if removal.returncode != 0:
        raise ExternalConflictError(
            "temporary base reference could not be removed",
            details={"repository_path": str(repository_path), "stderr": removal.stderr.strip()},
        )
    return branch


def _bound_tree(intake_path: Path, head_revision: str, *, max_entries: int, max_bytes: int) -> None:
    """Refuse a head whose recursive tree exceeds the entry or total-size bound.

    ``git ls-tree -r`` is streamed and stopped at the first violation, so a tree that expands
    from few objects into millions of entries (a git bomb) is never fully enumerated.
    """
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        f"--git-dir={intake_path}",
        "ls-tree",
        "-r",
        "-l",
        "-z",
        "--full-tree",
        head_revision,
    ]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            cwd=intake_path,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        timer = threading.Timer(GIT_TIMEOUT_SECONDS, process.kill)
        timer.start()
        entries = 0
        total = 0
        violation: ExternalConflictError | None = None
        pending = b""
        try:
            assert process.stdout is not None
            descriptor = process.stdout.fileno()
            while violation is None:
                chunk = os.read(descriptor, _TREE_READ_CHUNK)
                if not chunk:
                    break
                records = (pending + chunk).split(b"\0")
                pending = records.pop()
                if len(pending) > _TREE_MAX_RECORD_BYTES:
                    violation = ExternalConflictError(
                        "worker branch tree contains an oversized path",
                        details={"head_revision": head_revision},
                    )
                    break
                for record in records:
                    entries += 1
                    total += _tree_record_size(record)
                    if entries > max_entries or total > max_bytes:
                        violation = ExternalConflictError(
                            "worker branch tree exceeds the entry-count or size limit",
                            details={
                                "head_revision": head_revision,
                                "max_entries": max_entries,
                                "max_bytes": max_bytes,
                            },
                        )
                        break
        finally:
            timer.cancel()
            if violation is not None or process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if violation is not None:
            raise violation
        if process.returncode != 0:
            errors.seek(0)
            raise ExternalConflictError(
                "worker branch tree could not be listed",
                details={
                    "head_revision": head_revision,
                    "stderr": errors.read().decode("utf-8", "replace").strip(),
                },
            )


def _tree_record_size(record: bytes) -> int:
    """Blob size from one ``ls-tree -l -z`` record (``-`` for a submodule entry)."""
    header = record.partition(b"\t")[0].split()
    if len(header) != 4:
        raise ExternalConflictError("worker branch tree listing is malformed")
    return 0 if header[3] == b"-" else int(header[3])


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
