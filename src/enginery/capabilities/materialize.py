"""Immutable, content-addressed materialization of locked capabilities.

Materialization never overwrites capability bytes in place: a locked
capability is written once under its digest path, verified before the
write, and every read afterward resolves to the same bytes because the
path itself is a function of the digest. This mirrors the two-phase,
digest-verified publish protocol ``enginery.ledger.artifact_store`` uses
for run artifacts, kept independent of the ledger package so
``capabilities`` never depends on SQLite or the artifact store.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from enginery.capabilities.errors import CapabilityApprovalRequiredError, CapabilityIntegrityError
from enginery.capabilities.lock import CapabilityLock, LockedCapability
from enginery.domain.digests import Digest


def digest_path(root: Path, digest: Digest) -> Path:
    """The immutable, content-addressed path a digest resolves to under ``root``."""

    return root / digest.algorithm / digest.hex_value[:2] / digest.hex_value


def _publish_bytes(root: Path, data: bytes, digest: Digest) -> Path:
    target = digest_path(root, digest)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(dir=tmp_dir)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def materialize_capability(entry: LockedCapability, content: bytes, *, root: Path) -> Path:
    """Write one locked capability's bytes immutably under ``root``.

    Refuses bytes that do not hash to the locked digest; the caller must
    already hold an approved lock (see :func:`materialize_lock`) before
    calling this for a run-introduced capability.
    """

    if Digest.of_bytes(content) != entry.digest:
        raise CapabilityIntegrityError(
            "materialized bytes do not match the locked digest",
            details={"name": entry.name, "version": entry.version},
        )
    return _publish_bytes(root, content, entry.digest)


def materialize_lock(
    lock: CapabilityLock,
    content_by_digest: Mapping[Digest, bytes],
    *,
    root: Path,
    approved_names: frozenset[str] = frozenset(),
) -> Mapping[str, Path]:
    """Materialize every entry in ``lock`` and return ``name -> path``.

    Any entry :meth:`LockedCapability.requires_human_approval` reports
    ``True`` for must have its name in ``approved_names`` -- the caller's
    already-recorded, digest-bound ``capability.materialize`` approval --
    or materialization refuses to run for that entry rather than silently
    skipping or downgrading trust.
    """

    materialized: dict[str, Path] = {}
    for entry in lock.entries:
        if entry.requires_human_approval() and entry.name not in approved_names:
            raise CapabilityApprovalRequiredError(
                "capability requires interactive exact-digest human approval before it can execute",
                details={"name": entry.name, "version": entry.version},
            )
        content = content_by_digest.get(entry.digest)
        if content is None:
            raise CapabilityIntegrityError(
                "no content was supplied for a locked capability digest",
                details={"name": entry.name, "version": entry.version},
            )
        materialized[entry.name] = materialize_capability(entry, content, root=root)
    return materialized


__all__ = ["digest_path", "materialize_capability", "materialize_lock"]
