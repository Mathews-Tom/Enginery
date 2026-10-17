"""Independently verify tracked bytes using only host-owned Git metadata."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
from pathlib import Path

from enginery.adapters.git_reader import git_bytes as _git
from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    BuildIdentity,
    BuildKind,
    EnvironmentFingerprint,
    ToolVersion,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import StaleEvidenceError


def _tracked_bytes(root: int, relative: str) -> tuple[bytes, int]:
    """Traverse every component relative to pinned directories, never following links."""
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise StaleEvidenceError("behavioral_build_mismatch: invalid tracked path")
    directory = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise StaleEvidenceError("behavioral_build_mismatch: linked or nonregular input")
            data = stream.read(MAX_EVIDENCE_BYTES + 1)
            if len(data) > MAX_EVIDENCE_BYTES:
                raise StaleEvidenceError("behavioral checkout file exceeds the 1 MiB bound")
            return data, metadata.st_mode
    except OSError as error:
        raise StaleEvidenceError("behavioral_build_mismatch: unreadable tracked input") from error
    finally:
        os.close(directory)


def verified_returned_build(
    checkout: Path, repository: Path, expected_head: str
) -> BuildIdentity:
    """Verify returned bytes without executing any process in the returned tree.

    ``repository`` is host-owned and never mounted into the isolated runner. Only
    its object database supplies the expected listing; returned Git config is inert.
    """
    return _verified_tree(checkout, repository, expected_head, returned=True)


def _verified_tree(
    checkout: Path, repository: Path, expected_head: str, *, returned: bool
) -> BuildIdentity:
    listing = _git(repository, "ls-tree", "-r", "-z", expected_head + "^{tree}")
    records: list[dict[str, str]] = []
    root = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.stat(".git", dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if returned:
                raise StaleEvidenceError("behavioral_build_mismatch: returned Git metadata")
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            metadata, filename = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise StaleEvidenceError("behavioral_build_mismatch: nonregular tracked input")
            relative = filename.decode("utf-8")
            data, actual_mode = _tracked_bytes(root, relative)
            algorithm = "sha1" if len(object_id) == 40 else "sha256"
            header = f"blob {len(data)}\0".encode()
            if hashlib.new(algorithm, header + data).hexdigest() != object_id:
                raise StaleEvidenceError("behavioral_build_mismatch: checkout differs from bound tree")
            if bool(actual_mode & 0o111) != (mode == "100755"):
                raise StaleEvidenceError("behavioral_build_mismatch: executable mode differs")
            records.append({"path": relative, "mode": mode, "digest": str(Digest.of_bytes(data))})
    finally:
        os.close(root)
    return BuildIdentity(BuildKind.SOURCE_TREE, Digest.of_json(records))


def verified_build(checkout: Path, expected_head: str) -> BuildIdentity:
    """Verify a host-owned checkout before it is supplied to an observer."""
    actual = _git(checkout, "rev-parse", "--verify", "HEAD").decode().strip()
    if actual != expected_head:
        raise StaleEvidenceError("behavioral_build_mismatch: checkout HEAD differs from bound head")
    return _verified_tree(checkout, checkout, expected_head, returned=False)


def host_environment() -> EnvironmentFingerprint:
    """Retain a noncredential host receipt and the observer interpreter's real version."""
    receipt = Digest.of_json(
        {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "uid": os.getuid(),
            "python_executable": str(Path(sys.executable).resolve()),
        }
    )
    return EnvironmentFingerprint(receipt, (ToolVersion("python", platform.python_version()),))
