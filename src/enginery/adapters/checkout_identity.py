"""Independently verify the tracked bytes of a source checkout before observation."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

from enginery.application.git_environment import repository_isolated_environment
from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    BuildIdentity,
    BuildKind,
    EnvironmentFingerprint,
    ToolVersion,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import StaleEvidenceError


def _git(checkout: Path, *args: str) -> bytes:
    with subprocess.Popen(
        ("git", "-C", str(checkout), *args), stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=repository_isolated_environment(),
    ) as process:
        assert process.stdout is not None
        output = process.stdout.read(MAX_EVIDENCE_BYTES + 1)
        if len(output) > MAX_EVIDENCE_BYTES:
            process.kill()
            raise StaleEvidenceError("checkout identity exceeds the 1 MiB bound")
        if process.wait(timeout=30):
            raise StaleEvidenceError("could not verify the behavioral checkout identity")
        return output


def verified_build(checkout: Path, expected_head: str) -> BuildIdentity:
    """Hash actual tracked bytes only after matching every blob to the expected commit."""
    actual = _git(checkout, "rev-parse", "--verify", "HEAD").decode().strip()
    if actual != expected_head:
        raise StaleEvidenceError("behavioral_build_mismatch: checkout HEAD differs from bound head")
    if _git(checkout, "status", "--porcelain", "--untracked-files=no"):
        raise StaleEvidenceError("behavioral_build_mismatch: tracked checkout bytes are modified")
    records: list[dict[str, str]] = []
    for entry in _git(checkout, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not entry:
            continue
        metadata, filename = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise StaleEvidenceError("behavioral_build_mismatch: nonregular tracked input")
        relative = filename.decode("utf-8")
        path = checkout / relative
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise StaleEvidenceError("behavioral_build_mismatch: linked tracked input")
        with path.open("rb") as stream:
            data = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(data) > MAX_EVIDENCE_BYTES:
            raise StaleEvidenceError("behavioral checkout file exceeds the 1 MiB bound")
        header = f"blob {len(data)}\0".encode()
        algorithm = "sha1" if len(object_id) == 40 else "sha256"
        if hashlib.new(algorithm, header + data).hexdigest() != object_id:
            raise StaleEvidenceError("behavioral_build_mismatch: checkout differs from bound tree")
        if bool(path.stat().st_mode & 0o111) != (mode == "100755"):
            raise StaleEvidenceError("behavioral_build_mismatch: executable mode differs")
        records.append({"path": relative, "mode": mode, "digest": str(Digest.of_bytes(data))})
    if _git(checkout, "rev-parse", "--verify", "HEAD").decode().strip() != expected_head:
        raise StaleEvidenceError("behavioral_build_mismatch: HEAD changed during verification")
    return BuildIdentity(BuildKind.SOURCE_TREE, Digest.of_json(records))


def host_environment() -> EnvironmentFingerprint:
    """Retain a noncredential host receipt and the observer interpreter's real version."""
    receipt = Digest.of_json({
        "system": platform.system(), "release": platform.release(),
        "machine": platform.machine(), "uid": os.getuid(),
        "python_executable": str(Path(sys.executable).resolve()),
    })
    return EnvironmentFingerprint(receipt, (ToolVersion("python", platform.python_version()),))
