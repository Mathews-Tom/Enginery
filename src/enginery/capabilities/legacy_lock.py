"""Read-only model of the previous (single-blob) capability lock schema.

Before closures, a lock entry bound a capability name and version to the
digest of one fetched blob. Such an entry proves nothing about sibling
scripts, references, or dependencies, so it can never authorize
materialization: it is decoded here only so an operator can see what was
recorded and re-resolve it. It is deliberately not a
:class:`~enginery.capabilities.lock.CapabilityLock`, offers no path into
materialization or approval, and is never silently upgraded.
"""

from __future__ import annotations

from dataclasses import dataclass

from enginery.capabilities.lock import ProvenanceRecord
from enginery.domain.digests import Digest

LEGACY_LOCK_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LegacyLockedCapability:
    """One entry of the previous lock schema: a name bound to a single blob digest."""

    name: str
    version: str
    digest: Digest
    provenance: ProvenanceRecord
    license: str | None
    introduced_by_run: bool


@dataclass(frozen=True, slots=True)
class LegacyCapabilityLock:
    """An immutable, read-only legacy lock that requires re-resolution."""

    entries: tuple[LegacyLockedCapability, ...]

    def digest(self) -> Digest:
        """The digest the legacy schema computed, kept only for audit comparison."""

        return Digest.of_json(
            [
                {
                    "name": entry.name,
                    "version": entry.version,
                    "digest": str(entry.digest),
                    "provenance_status": entry.provenance.status.value,
                    "introduced_by_run": entry.introduced_by_run,
                }
                for entry in self.entries
            ]
        )


__all__ = ["LEGACY_LOCK_SCHEMA_VERSION", "LegacyCapabilityLock", "LegacyLockedCapability"]
