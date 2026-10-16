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
import tempfile
from dataclasses import dataclass
from pathlib import Path

from enginery.domain.errors import ExternalConflictError, InvalidInputError

OWNERSHIP_SCHEMA_VERSION = 1
OWNERSHIP_FILE = "setup-ownership.json"
CREATED_BY = "enginery factory init"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def refuse_links(root: Path, relative: Path, *, what: str) -> None:
    """Refuse a symbolic link on any existing component of ``relative`` below ``root``."""
    if ".." in relative.parts or relative.is_absolute():
        raise InvalidInputError(
            f"{what} must be a plain relative path", details={"path": str(relative)}
        )
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InvalidInputError(
                f"{what} is a symbolic link; refusing to read, write or remove through it",
                details={"path": str(current)},
            )
        if not os.path.lexists(current):
            return


def read_regular(path: Path, *, what: str) -> bytes | None:
    """The bytes of ``path``, ``None`` when absent; a link or non-file is refused."""
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise InvalidInputError(
            f"{what} exists but is not a regular file", details={"path": str(path)}
        )
    return path.read_bytes()


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
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def remove_regular(root: Path, relative: Path, *, what: str) -> None:
    """Remove one regular file below ``root``; links anywhere on its path are refused."""
    refuse_links(root, relative, what=what)
    (root / relative).unlink()


def write_private_atomically(path: Path, payload: bytes, *, what: str) -> None:
    """Replace one private record atomically (mode 0600) in its private directory."""
    directory = path.parent
    for component in (directory, path):
        if component.is_symlink():
            raise InvalidInputError(
                f"{what} is a symbolic link; refusing to write through it",
                details={"path": str(component)},
            )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".ownership-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
