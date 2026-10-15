"""``enginery capability lock --check``: on-disk materialized-store drift check.

This verifies that every capability an existing lockfile records still
has a materialized closure under the content-addressed store
(:func:`enginery.capabilities.materialize.digest_path`) that matches the
locked file records exactly -- no missing, changed, or extra file and no
symlink. It never re-fetches from a live source and never grants an
approval; it only detects drift in what was already immutably materialized
-- the on-disk analog of :class:`enginery.capabilities.errors.CapabilityLockDriftError`,
which catches drift at resolution time.

A lockfile in the previous single-blob schema cannot be checked against a
closure at all: every entry is reported as needing re-resolution rather than
being silently upgraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enginery.capabilities.errors import CapabilityIntegrityError
from enginery.capabilities.lock import LockedCapability
from enginery.capabilities.materialize import verify_materialized
from enginery.capabilities.serialization import (
    is_legacy_lock,
    legacy_lock_from_json,
    lock_from_json,
    read_lock_document,
)


@dataclass(frozen=True, slots=True)
class CapabilityDriftFinding:
    name: str
    version: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CapabilityLockCheckReport:
    lockfile: Path | None
    findings: tuple[CapabilityDriftFinding, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)


def check_lock(*, lockfile: Path, capabilities_root: Path) -> CapabilityLockCheckReport:
    """Verify a lockfile's entries against the materialized store, if the lockfile exists.

    A missing lockfile is not drift -- it means no capability has been
    locked yet, matching "the engine works with Armory disabled" and,
    more generally, with no capabilities configured at all.
    """

    if not lockfile.exists():
        return CapabilityLockCheckReport(lockfile=None, findings=())
    document = read_lock_document(lockfile)
    if is_legacy_lock(document):
        legacy = legacy_lock_from_json(document)
        findings = tuple(
            CapabilityDriftFinding(
                name=entry.name,
                version=entry.version,
                ok=False,
                detail="legacy single-blob lock entry has no closure; re-resolve the capability",
            )
            for entry in legacy.entries
        )
        return CapabilityLockCheckReport(lockfile=lockfile, findings=findings)
    lock = lock_from_json(document)
    findings = tuple(_check_entry(entry, capabilities_root) for entry in lock.entries)
    return CapabilityLockCheckReport(lockfile=lockfile, findings=findings)


def _check_entry(entry: LockedCapability, root: Path) -> CapabilityDriftFinding:
    try:
        verify_materialized(entry, root)
    except CapabilityIntegrityError as error:
        differences = error.details.get("differences")
        suffix = f" ({'; '.join(map(str, differences))})" if isinstance(differences, list) else ""
        return CapabilityDriftFinding(
            name=entry.name, version=entry.version, ok=False, detail=f"{error}{suffix}"
        )
    return CapabilityDriftFinding(
        name=entry.name, version=entry.version, ok=True, detail="matches the locked closure"
    )


__all__ = ["CapabilityDriftFinding", "CapabilityLockCheckReport", "check_lock"]
