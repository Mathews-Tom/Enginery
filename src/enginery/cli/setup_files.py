"""Link-safe file operations and the ownership record of guided setup.

Setup writes and deletes at most two kinds of file: the tracked project policy under the
project directory and its private ownership record under the private state directory. Every
write is exclusive (never over an existing file) or an atomic replace of a private record;
every path is absolute, free of ``..``, and refused when a symbolic link stands anywhere on
its way below the directory setup owns. The ownership record lists each file setup created
with the sha256 of what it wrote, so a later uninstall removes only files whose content
still matches.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from enginery.capabilities.atomic import fsync_directory
from enginery.cli.config_locations import MAX_CONFIG_BYTES
from enginery.domain.errors import ExternalConflictError, InvalidInputError

OWNERSHIP_SCHEMA_VERSION = 1
OWNERSHIP_FILE = "setup-ownership.json"
CREATED_BY = "enginery factory init"


def _typed_file_errors[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    """Translate filesystem failures after the operation has completed its rollback."""

    @wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return function(*args, **kwargs)
        except OSError as error:
            path = error.filename or (str(args[0]) if args else "<unknown>")
            failure = (
                ExternalConflictError if isinstance(error, FileExistsError) else InvalidInputError
            )
            raise failure(
                f"setup could not access {path}: {error.strerror or error}",
                details={"path": str(path)},
            ) from error

    return guarded


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@_typed_file_errors
def refuse_links(root: Path, relative: Path, *, what: str) -> None:
    """Refuse a symbolic link on any existing component of ``relative`` below ``root``."""
    if ".." in relative.parts or relative.is_absolute():
        raise InvalidInputError(
            f"{what} must be a plain relative path", details={"path": str(relative)}
        )
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise InvalidInputError(
                f"{what} is a symbolic link; refusing to read, write or remove through it",
                details={"path": str(current)},
            )
        if current != root / relative and not stat.S_ISDIR(mode):
            raise InvalidInputError(
                f"{what} has a non-directory ancestor", details={"path": str(current)}
            )


@_typed_file_errors
def read_regular(path: Path, *, what: str) -> bytes | None:
    """The bytes of ``path``, ``None`` when absent; a link or non-file is refused."""
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise InvalidInputError(
            f"{what} exists but is not a regular file", details={"path": str(path)}
        )
    with path.open("rb") as stream:
        payload = stream.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise InvalidInputError(
            f"{what} at {path} exceeds the {MAX_CONFIG_BYTES}-byte limit",
            details={"path": str(path)},
        )
    return payload


@_typed_file_errors
def create_exclusive(root: Path, relative: Path, payload: bytes, *, what: str) -> Path:
    """Create ``root/relative`` with ``payload``; refuse links and never replace a file."""
    refuse_links(root, relative, what=what)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    refuse_links(root, relative, what=what)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o644)
    except FileExistsError as error:
        raise ExternalConflictError(
            f"{what} appeared while setup was running; it is user-owned and was not overwritten",
            details={"path": str(target)},
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


@_typed_file_errors
def remove_regular(root: Path, relative: Path, *, what: str) -> None:
    """Remove one regular file below ``root``; links anywhere on its path are refused."""
    refuse_links(root, relative, what=what)
    (root / relative).unlink()


@_typed_file_errors
def require_private_path(path: Path, *, what: str) -> None:
    """Inspect every owned component below the private state root with lstat."""
    root = path.parents[3]
    refuse_links(root, path.relative_to(root), what=what)
    read_regular(path, what=what)


@_typed_file_errors
def write_private_atomically(path: Path, payload: bytes, *, what: str) -> None:
    """Replace one private record atomically (mode 0600) in its private directory."""
    require_private_path(path, what=what)
    directory = path.parent
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    previous = read_regular(path, what=what)
    created: list[Path] = []
    temporary: Path | None = None
    backup: Path | None = None
    replaced = False
    try:
        for component in reversed(missing):
            component.mkdir(mode=0o700)
            created.append(component)
        require_private_path(path, what=what)
        if previous is not None:
            descriptor, name = tempfile.mkstemp(dir=directory, prefix=".ownership-backup-")
            backup = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
        descriptor, name = tempfile.mkstemp(dir=directory, prefix=".ownership-", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        require_private_path(path, what=what)
        os.replace(temporary, path)
        replaced = True
        fsync_directory(directory)
    except BaseException:
        if replaced:
            if backup is None:
                path.unlink()
            else:
                os.replace(backup, path)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
        for component in reversed(created):
            component.rmdir()
        raise
    finally:
        if backup is not None:
            backup.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class OwnedFile:
    """One file setup created: where, and the sha256 of exactly what it wrote."""

    path: str
    sha256: str
    role: str


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    """The private record of what setup created for one project."""

    project: str
    files: tuple[OwnedFile, ...]

    def owns(self, path: str) -> OwnedFile | None:
        return next((item for item in self.files if item.path == path), None)

    def with_file(self, owned: OwnedFile) -> OwnershipRecord:
        kept = tuple(item for item in self.files if item.path != owned.path)
        return OwnershipRecord(project=self.project, files=(*kept, owned))

    def without(self, path: str) -> OwnershipRecord:
        return OwnershipRecord(
            project=self.project, files=tuple(item for item in self.files if item.path != path)
        )

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": OWNERSHIP_SCHEMA_VERSION,
            "created_by": CREATED_BY,
            "project": self.project,
            "files": [
                {"path": item.path, "sha256": item.sha256, "role": item.role}
                for item in sorted(self.files, key=lambda item: item.path)
            ],
        }

    def to_bytes(self) -> bytes:
        return (json.dumps(self.to_state(), sort_keys=True, indent=2) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes, *, source: str) -> OwnershipRecord:
        try:
            state = json.loads(raw)
        except ValueError as error:
            raise InvalidInputError(
                "the setup ownership record is not valid JSON; remove it only if you accept "
                "that setup will no longer recognise the files it created",
                details={"path": source},
            ) from error
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != OWNERSHIP_SCHEMA_VERSION
            or not isinstance(state.get("project"), str)
            or not isinstance(state.get("files"), list)
        ):
            raise InvalidInputError(
                "the setup ownership record has an unsupported shape", details={"path": source}
            )
        files: list[OwnedFile] = []
        for item in state["files"]:
            if not (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("sha256"), str)
                and isinstance(item.get("role"), str)
            ):
                raise InvalidInputError(
                    "the setup ownership record has a malformed file entry",
                    details={"path": source},
                )
            files.append(OwnedFile(item["path"], item["sha256"], item["role"]))
        return cls(project=state["project"], files=tuple(files))


def load_ownership(path: Path, *, project: Path) -> OwnershipRecord | None:
    """The ownership record at ``path``, ``None`` when setup never created one."""
    raw = read_regular(path, what="the setup ownership record")
    if raw is None:
        return None
    record = OwnershipRecord.from_bytes(raw, source=str(path))
    if record.project != str(project):
        raise InvalidInputError(
            "the setup ownership record belongs to a different project path",
            details={"path": str(path), "record_project": record.project},
        )
    return record


__all__ = [
    "CREATED_BY",
    "OWNERSHIP_FILE",
    "OwnedFile",
    "OwnershipRecord",
    "create_exclusive",
    "load_ownership",
    "read_regular",
    "refuse_links",
    "remove_regular",
    "sha256_bytes",
    "write_private_atomically",
]
