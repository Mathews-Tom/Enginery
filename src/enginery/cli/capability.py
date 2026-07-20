"""``enginery capability lock --check``: on-disk materialized-store drift check.

This verifies that every capability an existing lockfile records still
has matching bytes under the content-addressed materialized store
(:func:`enginery.capabilities.materialize.digest_path`). It never
re-fetches from a live source and never grants an approval; it only
detects drift in what was already immutably materialized -- the on-disk
analog of :class:`enginery.capabilities.errors.CapabilityLockDriftError`,
which catches drift at resolution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enginery.capabilities.lock import LockedCapability
from enginery.capabilities.materialize import digest_path
from enginery.capabilities.serialization import read_lock
from enginery.domain.digests import Digest


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
    lock = read_lock(lockfile)
    findings = tuple(_check_entry(entry, capabilities_root) for entry in lock.entries)
    return CapabilityLockCheckReport(lockfile=lockfile, findings=findings)


def _check_entry(entry: LockedCapability, root: Path) -> CapabilityDriftFinding:
    path = digest_path(root, entry.digest)
    if not path.exists():
        return CapabilityDriftFinding(
            name=entry.name,
            version=entry.version,
            ok=False,
            detail="materialized bytes are missing from the capabilities root",
        )
    actual = Digest.of_bytes(path.read_bytes())
    if actual != entry.digest:
        return CapabilityDriftFinding(
            name=entry.name,
            version=entry.version,
            ok=False,
            detail="on-disk bytes no longer match the locked digest",
        )
    return CapabilityDriftFinding(
        name=entry.name, version=entry.version, ok=True, detail="matches the locked digest"
    )


__all__ = ["CapabilityDriftFinding", "CapabilityLockCheckReport", "check_lock"]
