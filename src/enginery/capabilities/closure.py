"""Complete package asset closures: the unit capability trust is bound to.

A capability is a package directory, not a single file. Its *closure* is
every file the package ships -- instructions, references, scripts, data, and
its manifest -- plus the transitive dependency pins its manifest declares.
The canonical closure digest covers each file's normalized relative POSIX
path, asset kind, SHA-256, size, and executable bit, and every dependency
pin, so a changed sibling script, reference, or dependency pin changes the
digest and supersedes any approval bound to the old one.

The builders here are the boundary where untrusted file lists (from a
source adapter, or from a directory on disk) become a closure. They reject,
loudly and with an explicit error class, anything that could hide runnable
content from the digest: symlinks, hardlinked or non-regular files, absolute
or traversing paths, duplicate or case-colliding paths, unreviewed asset
types, executable bits on non-scripts, and packages beyond the size limits.
Nothing here ever imports, executes, or interprets package content.
"""

from __future__ import annotations

import enum
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.errors import (
    CapabilityAssetTypeError,
    CapabilityClosureLimitError,
    CapabilityManifestError,
    CapabilityPathError,
    CapabilitySymlinkError,
)
from enginery.capabilities.manifest import (
    MANIFEST_FILE_NAME,
    DependencyPin,
    PackageManifest,
    digest_from_text,
    parse_manifest,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

CLOSURE_SCHEMA_VERSION = 1
MAX_FILE_COUNT = 512
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PATH_LENGTH = 255
MAX_PATH_DEPTH = 12

_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_LICENSE_STEMS = frozenset({"license", "licence", "copying", "notice"})
_LICENSE_EXTENSIONS = frozenset({"", ".md", ".txt", ".rst"})
_SCRIPT_EXTENSIONS = frozenset({".py", ".sh", ".js", ".mjs", ".ts"})
_REFERENCE_EXTENSIONS = frozenset({".md", ".txt", ".rst"})
_DATA_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".csv", ".toml", ".txt"})
_DATA_DIRECTORIES = frozenset({"data", "schemas", "evals"})


class AssetKind(enum.StrEnum):
    """The reviewed asset types a package closure may contain."""

    INSTRUCTION = "instruction"
    REFERENCE = "reference"
    SCRIPT = "script"
    DATA = "data"
    METADATA = "metadata"


def normalize_path(raw: str) -> str:
    """Return ``raw`` if it is a safe, already-normalized relative POSIX path.

    Nothing is silently rewritten: ``./a``, ``a//b``, ``..`` segments,
    absolute paths, drive letters, backslashes, control characters, and
    non-NFC Unicode are all rejected rather than normalized.
    """

    problem = _path_problem(raw)
    if problem is not None:
        raise CapabilityPathError(
            f"package path is unsafe: {problem}", details={"path": raw[:MAX_PATH_LENGTH]}
        )
    return raw


def _path_problem(raw: str) -> str | None:
    if not raw:
        return "empty path"
    if len(raw) > MAX_PATH_LENGTH:
        return "path is too long"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        return "path contains control characters"
    if "\\" in raw:
        return "path contains a backslash"
    if raw.startswith("/") or _DRIVE_PATTERN.match(raw):
        return "path is absolute"
    if unicodedata.normalize("NFC", raw) != raw:
        return "path is not NFC-normalized"
    parts = raw.split("/")
    if len(parts) > MAX_PATH_DEPTH:
        return "path is too deep"
    if any(part in {"", ".", ".."} for part in parts):
        return "path has an empty, '.', or '..' segment"
    return None


def is_license_file_name(path: str) -> bool:
    """Whether ``path`` is a root license-style file.

    The name stem (before the first dot) is ``license``, ``licence``,
    ``copying`` or ``notice`` in any case, with no extension or ``.md``,
    ``.txt`` or ``.rst``. Nested paths never match.
    """

    if "/" in path:
        return False
    stem, dot, rest = path.partition(".")
    return stem.casefold() in _LICENSE_STEMS and (dot + rest).casefold() in _LICENSE_EXTENSIONS


def classify_asset(path: str, *, executable: bool = False) -> AssetKind:
    """Map a normalized path to its reviewed asset kind, or reject it.

    The mapping is by location and extension only (never by content), and
    deliberately small: a file outside these shapes is an *unreviewed asset
    type* and is refused rather than hashed and shipped.
    """

    parts = path.split("/")
    name = parts[-1]
    suffix = posixpath.splitext(name)[1].lower()
    kind = _kind_for(parts, name, suffix)
    if kind is None:
        raise CapabilityAssetTypeError(
            "package contains an unreviewed asset type", details={"path": path}
        )
    if executable and kind is not AssetKind.SCRIPT:
        raise CapabilityAssetTypeError(
            "only a script asset may carry an executable bit", details={"path": path}
        )
    return kind


def _kind_for(parts: list[str], name: str, suffix: str) -> AssetKind | None:
    if len(parts) == 1:
        if name == MANIFEST_FILE_NAME or is_license_file_name(name):
            return AssetKind.METADATA
        return AssetKind.INSTRUCTION if suffix == ".md" else None
    top = parts[0]
    if top == "instructions" and suffix == ".md":
        return AssetKind.INSTRUCTION
    if top == "references" and suffix in _REFERENCE_EXTENSIONS:
        return AssetKind.REFERENCE
    if top == "scripts" and suffix in _SCRIPT_EXTENSIONS:
        return AssetKind.SCRIPT
    if top in _DATA_DIRECTORIES and suffix in _DATA_EXTENSIONS:
        return AssetKind.DATA
    return None


@dataclass(frozen=True, slots=True)
class AssetRecord:
    """One file of a package closure: path, kind, digest, size, executable bit."""

    path: str
    kind: AssetKind
    digest: Digest
    size: int
    executable: bool

    def __post_init__(self) -> None:
        normalize_path(self.path)
        if self.size < 0:
            raise InvalidInputError("asset size must not be negative")
        if self.executable and self.kind is not AssetKind.SCRIPT:
            raise CapabilityAssetTypeError(
                "only a script asset may carry an executable bit", details={"path": self.path}
            )

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "digest": str(self.digest),
            "size": self.size,
            "executable": self.executable,
        }

    @classmethod
    def from_json(cls, data: object) -> AssetRecord:
        if not isinstance(data, dict):
            raise InvalidInputError("asset record must be a JSON object")
        path, kind, digest = data.get("path"), data.get("kind"), data.get("digest")
        size, executable = data.get("size"), data.get("executable")
        if not isinstance(path, str) or not isinstance(kind, str) or not isinstance(digest, str):
            raise InvalidInputError("asset record path, kind, and digest must be strings")
        if not isinstance(size, int) or isinstance(size, bool):
            raise InvalidInputError("asset record size must be an integer")
        if not isinstance(executable, bool):
            raise InvalidInputError("asset record executable must be a boolean")
        return cls(
            path=path,
            kind=AssetKind(kind),
            digest=digest_from_text(digest),
            size=size,
            executable=executable,
        )


def closure_digest(files: Sequence[AssetRecord], dependencies: Sequence[DependencyPin]) -> Digest:
    """The canonical digest over every asset record and dependency pin.

    Ordering is fixed (files by path, pins by name) so equal closures hash
    equally however they were assembled.
    """

    ordered_files = sorted(files, key=lambda record: record.path)
    ordered_pins = sorted(dependencies, key=lambda pin: pin.name)
    payload = {
        "schema": CLOSURE_SCHEMA_VERSION,
        "files": [record.to_json() for record in ordered_files],
        "dependencies": [pin.to_json() for pin in ordered_pins],
    }
    return Digest.of_json(payload)


@dataclass(frozen=True, slots=True)
class PackageClosure:
    """A verified package: its parsed manifest and every file record."""

    manifest: PackageManifest
    files: tuple[AssetRecord, ...]

    @property
    def digest(self) -> Digest:
        return closure_digest(self.files, self.manifest.dependencies)


def build_closure(files: Iterable[PackageFile]) -> PackageClosure:
    """Validate an untrusted file list and compute its closure.

    Raises an explicit :class:`~enginery.capabilities.errors.CapabilityError`
    subclass for any unsafe path, collision, unreviewed asset type, limit
    breach, or missing/invalid manifest.
    """

    supplied = tuple(files)
    if len(supplied) > MAX_FILE_COUNT:
        raise CapabilityClosureLimitError(
            "package has too many files",
            details={"count": len(supplied), "limit": MAX_FILE_COUNT},
        )
    records: list[AssetRecord] = []
    collision_keys: dict[str, str] = {}
    manifest_bytes: bytes | None = None
    total = 0
    for item in supplied:
        path = normalize_path(item.path)
        key = unicodedata.normalize("NFC", path).casefold()
        if key in collision_keys:
            raise CapabilityPathError(
                "package contains duplicate or case-colliding paths",
                details={"path": path, "other": collision_keys[key]},
            )
        collision_keys[key] = path
        size = len(item.content)
        if size > MAX_FILE_BYTES:
            raise CapabilityClosureLimitError(
                "package file is too large",
                details={"path": path, "size": size, "limit": MAX_FILE_BYTES},
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise CapabilityClosureLimitError(
                "package is too large", details={"limit": MAX_TOTAL_BYTES}
            )
        kind = classify_asset(path, executable=item.executable)
        records.append(
            AssetRecord(
                path=path,
                kind=kind,
                digest=Digest.of_bytes(item.content),
                size=size,
                executable=item.executable,
            )
        )
        if path == MANIFEST_FILE_NAME:
            manifest_bytes = item.content
    _reject_file_directory_conflicts(collision_keys)
    if manifest_bytes is None:
        raise CapabilityManifestError(
            "package has no manifest", details={"expected": MANIFEST_FILE_NAME}
        )
    return PackageClosure(
        manifest=parse_manifest(manifest_bytes),
        files=tuple(sorted(records, key=lambda record: record.path)),
    )


def _reject_file_directory_conflicts(collision_keys: dict[str, str]) -> None:
    directories = {
        key.rsplit("/", maxsplit=index)[0]
        for key in collision_keys
        for index in range(1, key.count("/") + 1)
    }
    for key, path in collision_keys.items():
        if key in directories:
            raise CapabilityPathError(
                "package path is both a file and a directory", details={"path": path}
            )


def read_package_directory(directory: Path) -> tuple[PackageFile, ...]:
    """Read every file under ``directory`` without ever following a link.

    The directory itself must not be a symlink. Any symlink, hardlinked file
    (link count above one), or non-regular entry (device, socket, FIFO) is
    rejected. Files are opened ``O_NOFOLLOW`` and re-checked after opening,
    so a swap between listing and reading cannot smuggle a link through.
    Ordering is deterministic.
    """

    try:
        root_stat = os.lstat(directory)
    except OSError as error:
        raise CapabilityPathError(
            "package directory cannot be read", details={"path": str(directory)}
        ) from error
    if stat.S_ISLNK(root_stat.st_mode):
        raise CapabilitySymlinkError(
            "package directory is a symlink", details={"path": str(directory)}
        )
    if not stat.S_ISDIR(root_stat.st_mode):
        raise CapabilityPathError(
            "package path is not a directory", details={"path": str(directory)}
        )
    collected: list[PackageFile] = []
    total = [0]
    _walk(directory, "", collected, total, depth=0)
    return tuple(collected)


def _walk(
    directory: Path, prefix: str, collected: list[PackageFile], total: list[int], *, depth: int
) -> None:
    if depth > MAX_PATH_DEPTH:
        raise CapabilityPathError("package directory is too deep", details={"path": prefix})
    with os.scandir(directory) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        relative = f"{prefix}{entry.name}"
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise CapabilitySymlinkError("package contains a symlink", details={"path": relative})
        if stat.S_ISDIR(info.st_mode):
            _walk(Path(entry.path), f"{relative}/", collected, total, depth=depth + 1)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise CapabilitySymlinkError(
                "package contains a non-regular file", details={"path": relative}
            )
        collected.append(_read_regular_file(Path(entry.path), relative, collected, total))


def _read_regular_file(
    path: Path, relative: str, collected: list[PackageFile], total: list[int]
) -> PackageFile:
    if len(collected) >= MAX_FILE_COUNT:
        raise CapabilityClosureLimitError(
            "package has too many files", details={"limit": MAX_FILE_COUNT}
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CapabilitySymlinkError(
            "package file cannot be opened without following links", details={"path": relative}
        ) from error
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CapabilitySymlinkError(
                "package contains a non-regular file", details={"path": relative}
            )
        if info.st_nlink > 1:
            raise CapabilitySymlinkError(
                "package contains a hardlinked file", details={"path": relative}
            )
        if info.st_size > MAX_FILE_BYTES:
            raise CapabilityClosureLimitError(
                "package file is too large", details={"path": relative, "limit": MAX_FILE_BYTES}
            )
        total[0] += info.st_size
        if total[0] > MAX_TOTAL_BYTES:
            raise CapabilityClosureLimitError(
                "package is too large", details={"limit": MAX_TOTAL_BYTES}
            )
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise CapabilityClosureLimitError(
            "package file is too large", details={"path": relative, "limit": MAX_FILE_BYTES}
        )
    return PackageFile(path=relative, content=content, executable=bool(info.st_mode & 0o111))


def build_closure_from_directory(directory: Path) -> PackageClosure:
    """Read a package directory and compute its closure."""

    return build_closure(read_package_directory(directory))


__all__ = [
    "CLOSURE_SCHEMA_VERSION",
    "MAX_FILE_BYTES",
    "MAX_FILE_COUNT",
    "MAX_PATH_DEPTH",
    "MAX_PATH_LENGTH",
    "MAX_TOTAL_BYTES",
    "AssetKind",
    "AssetRecord",
    "PackageClosure",
    "build_closure",
    "build_closure_from_directory",
    "classify_asset",
    "closure_digest",
    "is_license_file_name",
    "normalize_path",
    "read_package_directory",
]
