"""Immutable, content-addressed materialization of locked package closures.

Materialization never overwrites capability files in place: a locked
closure is verified against its lock entry, written once as a whole
directory under its closure-digest path, and every later read re-verifies
that directory against the lock entry before use. The path itself is a
function of the closure digest, so one digest can only ever name one set of
files. This mirrors the two-phase, digest-verified publish protocol
``enginery.ledger.artifact_store`` uses for run artifacts, kept independent
of the ledger package so ``capabilities`` never depends on SQLite or the
artifact store.

Package content is only ever written as inert files; nothing here imports,
executes, or interprets it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.closure import (
    AssetRecord,
    build_closure,
    build_closure_from_directory,
)
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
)
from enginery.capabilities.lock import CapabilityLock, LockedCapability
from enginery.domain.digests import Digest

_MAX_REPORTED_DIFFERENCES = 10


def digest_path(root: Path, digest: Digest) -> Path:
    """The immutable, content-addressed directory a closure digest resolves to under ``root``."""

    return root / digest.algorithm / digest.hex_value[:2] / digest.hex_value


def closure_differences(
    expected: Sequence[AssetRecord], actual: Sequence[AssetRecord]
) -> list[str]:
    """Human-readable differences between a locked and an observed file set (bounded)."""

    wanted = {record.path: record for record in expected}
    found = {record.path: record for record in actual}
    differences = [f"missing: {path}" for path in sorted(wanted.keys() - found.keys())]
    differences += [f"unexpected: {path}" for path in sorted(found.keys() - wanted.keys())]
    differences += [
        f"changed: {path}"
        for path in sorted(wanted.keys() & found.keys())
        if wanted[path] != found[path]
    ]
    return differences[:_MAX_REPORTED_DIFFERENCES]


def _mismatch_details(entry: LockedCapability, actual: Sequence[AssetRecord]) -> dict[str, object]:
    return {
        "name": entry.name,
        "version": entry.version,
        "differences": closure_differences(entry.files, actual),
    }


def _require_matching_files(entry: LockedCapability, files: Sequence[PackageFile]) -> None:
    closure = build_closure(files)
    if closure.digest != entry.digest or closure.files != entry.files:
        raise CapabilityIntegrityError(
            "supplied package files do not match the locked closure",
            details=_mismatch_details(entry, closure.files),
        )


def verify_materialized(entry: LockedCapability, root: Path) -> Path:
    """Re-verify a materialized closure against its lock entry and return its directory.

    Rejects a missing directory, any extra, missing, or changed file, and
    any symlink or non-regular entry anywhere inside it. Call this
    immediately before using materialized content.
    """

    target = digest_path(root, entry.digest)
    if not os.path.lexists(target):
        raise CapabilityIntegrityError(
            "materialized closure is missing from the capabilities root",
            details={"name": entry.name, "version": entry.version},
        )
    try:
        closure = build_closure_from_directory(target)
    except CapabilityError as error:
        raise CapabilityIntegrityError(
            "materialized closure is no longer a valid package",
            details={
                "name": entry.name,
                "version": entry.version,
                "reason": str(error),
                "reason_details": dict(error.details),
            },
        ) from error
    if closure.digest != entry.digest or closure.files != entry.files:
        raise CapabilityIntegrityError(
            "materialized closure no longer matches the locked closure",
            details=_mismatch_details(entry, closure.files),
        )
    return target


def _write_closure(directory: Path, files: Sequence[PackageFile]) -> None:
    for item in files:
        destination = directory / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(item.content)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o555 if item.executable else 0o444)


def _publish_closure(root: Path, entry: LockedCapability, files: Sequence[PackageFile]) -> Path:
    target = digest_path(root, entry.digest)
    if os.path.lexists(target):
        return verify_materialized(entry, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=tmp_dir))
    try:
        _write_closure(staging, files)
        try:
            os.rename(staging, target)
        except OSError:
            if not os.path.lexists(target):
                raise
            return verify_materialized(entry, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def materialize_capability(
    entry: LockedCapability, files: Sequence[PackageFile], *, root: Path
) -> Path:
    """Write one locked closure's files immutably under ``root`` and return its directory.

    Refuses files that do not rebuild to exactly the locked closure; the
    caller must already hold an approved lock (see :func:`materialize_lock`)
    before calling this for a run-introduced capability. An existing
    directory is re-verified and never overwritten.
    """

    _require_matching_files(entry, files)
    return _publish_closure(root, entry, files)


def materialize_lock(
    lock: CapabilityLock,
    files_by_name: Mapping[str, Sequence[PackageFile]],
    *,
    root: Path,
    approved_names: frozenset[str] = frozenset(),
) -> Mapping[str, Path]:
    """Materialize every entry in ``lock`` and return ``name -> directory``.

    Every entry is checked before any file is written. An entry
    :meth:`LockedCapability.requires_human_approval` reports ``True`` for must
    have its name in ``approved_names`` -- the caller's already-recorded,
    digest-bound ``capability.materialize`` approval -- and an entry whose
    license is held for redistribution is refused, rather than silently
    skipping or downgrading trust. Dependencies are lock entries of their own
    and are checked package by package.
    """

    for entry in lock.entries:
        if entry.requires_human_approval() and entry.name not in approved_names:
            raise CapabilityApprovalRequiredError(
                "capability requires interactive exact-digest human approval before it can execute",
                details={"name": entry.name, "version": entry.version},
            )
        if entry.redistribution_blocked():
            raise CapabilityLicenseHoldError(
                "capability license is not cleared for redistribution",
                details={
                    "name": entry.name,
                    "version": entry.version,
                    "license": entry.license,
                    "disposition": entry.license_disposition.value,
                },
            )
        files = files_by_name.get(entry.name)
        if files is None:
            raise CapabilityIntegrityError(
                "no files were supplied for a locked capability",
                details={"name": entry.name, "version": entry.version},
            )
        _require_matching_files(entry, files)
    return {
        entry.name: _publish_closure(root, entry, files_by_name[entry.name])
        for entry in lock.entries
    }


__all__ = [
    "closure_differences",
    "digest_path",
    "materialize_capability",
    "materialize_lock",
    "verify_materialized",
]
