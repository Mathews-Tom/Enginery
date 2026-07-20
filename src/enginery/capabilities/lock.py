"""Immutable, content-addressed capability locks and provenance classification.

A capability lock binds every capability a run may execute to an exact
content digest, a provenance classification, and whether the active run
introduced or changed it. Locking a set of capabilities produces one
canonical digest (:meth:`CapabilityLock.digest`) suitable for a run's
``capability_lock_digest`` and the policy layer's ``capability_locks``
input; any later resolution that would change a locked entry's digest is
drift, not a silent update (see ``enginery.capabilities.resolver``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


class ProvenanceStatus(enum.StrEnum):
    """How a capability's origin was established before it could execute.

    Content addressing (a matching digest) never upgrades this status by
    itself: a digest only proves bytes were not altered in transit, not who
    authored them. ``LOCAL_TRUSTED`` applies only to a capability already
    present in the bound, reviewed base revision. ``AUTHENTICATED`` requires
    a signature that verified against a pinned publisher key. Every other
    case, including a bare TLS fetch with no signature, is
    ``UNAUTHENTICATED`` and can execute only after interactive human
    exact-digest approval.
    """

    LOCAL_TRUSTED = "local_trusted"
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """The provenance evidence backing one locked capability."""

    status: ProvenanceStatus
    source_label: str
    signer_key_id: str | None
    verified_at: datetime

    def __post_init__(self) -> None:
        if not self.source_label.strip():
            raise InvalidInputError("provenance source_label must be non-blank")
        if self.status is ProvenanceStatus.AUTHENTICATED and self.signer_key_id is None:
            raise InvalidInputError("an authenticated provenance record requires a signer_key_id")
        if self.verified_at.tzinfo is None:
            raise InvalidInputError("provenance verified_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LockedCapability:
    """One capability bound to an exact digest for the lifetime of a lock."""

    name: str
    version: str
    digest: Digest
    provenance: ProvenanceRecord
    license: str | None
    introduced_by_run: bool

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise InvalidInputError("locked capability name and version must be non-blank")
        is_local_trusted = self.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
        if self.introduced_by_run and is_local_trusted:
            raise InvalidInputError(
                "a run-introduced capability cannot carry local_trusted provenance"
            )
        if not self.introduced_by_run and not is_local_trusted:
            raise InvalidInputError(
                "a reviewed-base-revision capability must carry local_trusted provenance"
            )

    def requires_human_approval(self) -> bool:
        """Only reviewed-base or signature-verified capabilities skip approval."""

        return self.introduced_by_run or self.provenance.status is ProvenanceStatus.UNAUTHENTICATED


@dataclass(frozen=True, slots=True)
class CapabilityLock:
    """An immutable set of locked capabilities bound to one run."""

    entries: tuple[LockedCapability, ...]

    def __post_init__(self) -> None:
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)):
            raise InvalidInputError("capability lock entries must have unique names")
        ordered = tuple(sorted(self.entries, key=lambda entry: entry.name))
        object.__setattr__(self, "entries", ordered)

    def get(self, name: str) -> LockedCapability | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def digest(self) -> Digest:
        """A canonical digest over every locked entry's trust-relevant fields."""

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

    def pending_approval(self) -> tuple[LockedCapability, ...]:
        return tuple(entry for entry in self.entries if entry.requires_human_approval())


__all__ = [
    "CapabilityLock",
    "LockedCapability",
    "ProvenanceRecord",
    "ProvenanceStatus",
]
