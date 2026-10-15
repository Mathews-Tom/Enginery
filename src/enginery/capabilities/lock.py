"""Immutable, closure-addressed capability locks and provenance classification.

A capability lock binds every capability a run may execute to the exact
closure digest of its complete package (see ``enginery.capabilities.closure``),
the origin it was read from (source label and immutable revision), its
license, a provenance classification, and whether the active run introduced
or changed it. Locking a set of capabilities produces one canonical digest
(:meth:`CapabilityLock.digest`) suitable for a run's ``capability_lock_digest``
and the policy layer's ``capability_locks`` input. Every one of those fields
is bound into that digest, so a change to any of them -- a sibling script, a
reference, a dependency pin, the license, the origin revision, or the trust
classification -- supersedes any approval recorded against the old digest.
Any later resolution that would change a locked entry's closure digest is
drift, not a silent update (see ``enginery.capabilities.resolver``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from enginery.capabilities.closure import AssetRecord, closure_digest
from enginery.capabilities.license import LicenseDisposition, classify_license
from enginery.capabilities.manifest import (
    CapabilityPermission,
    DependencyPin,
    PackageCompatibility,
    PackageKind,
    validate_name,
    validate_version,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


class ProvenanceStatus(enum.StrEnum):
    """How a capability's origin was established before it could execute.

    Content addressing (a matching digest) never upgrades this status by
    itself: a digest only proves files were not altered in transit, not who
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
    """One package closure bound to an exact digest for the lifetime of a lock.

    ``digest`` is the closure digest and must equal the canonical digest of
    ``files`` and ``dependencies``; a decoded entry whose records were edited
    without recomputing it is rejected at construction.
    """

    name: str
    version: str
    kind: PackageKind
    revision: str
    digest: Digest
    provenance: ProvenanceRecord
    license: str | None
    introduced_by_run: bool
    files: tuple[AssetRecord, ...]
    dependencies: tuple[DependencyPin, ...] = ()
    permissions: tuple[CapabilityPermission, ...] = ()
    compatibility: PackageCompatibility = field(default_factory=PackageCompatibility)

    def __post_init__(self) -> None:
        validate_name(self.name)
        validate_version(self.version)
        if not self.revision.strip():
            raise InvalidInputError("locked capability revision must be non-blank")
        is_local_trusted = self.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
        if self.introduced_by_run and is_local_trusted:
            raise InvalidInputError(
                "a run-introduced capability cannot carry local_trusted provenance"
            )
        if not self.introduced_by_run and not is_local_trusted:
            raise InvalidInputError(
                "a reviewed-base-revision capability must carry local_trusted provenance"
            )
        if not self.files:
            raise InvalidInputError("a locked capability must record at least one file")
        object.__setattr__(self, "files", tuple(sorted(self.files, key=lambda record: record.path)))
        object.__setattr__(
            self, "dependencies", tuple(sorted(self.dependencies, key=lambda pin: pin.name))
        )
        object.__setattr__(self, "permissions", tuple(sorted(set(self.permissions))))
        if closure_digest(self.files, self.dependencies) != self.digest:
            raise InvalidInputError(
                "locked closure digest does not match its recorded files and dependencies",
                details={"name": self.name, "version": self.version},
            )

    def requires_human_approval(self) -> bool:
        """Only reviewed-base capabilities skip approval; a signature never does."""

        return self.introduced_by_run or self.provenance.status is ProvenanceStatus.UNAUTHENTICATED

    @property
    def license_disposition(self) -> LicenseDisposition:
        """The declared license's redistribution classification for this package."""

        return classify_license(self.license)

    def redistribution_blocked(self) -> bool:
        """Whether a license hold stops this package from being redistributed.

        The check is per package. A package already present in the reviewed
        base revision is the repository's own content and is not
        redistributed by installing it into a task-local root, so only
        entries the run introduces are held; every one of those fails closed
        on an unknown, absent, or unreviewed license.
        """

        return self.introduced_by_run and self.license_disposition.is_hold

    def pin(self) -> DependencyPin:
        """This closure as a pin another package can declare."""

        return DependencyPin(name=self.name, version=self.version, closure_digest=self.digest)


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
        by_name = {entry.name: entry for entry in ordered}
        for entry in ordered:
            for pin in entry.dependencies:
                locked = by_name.get(pin.name)
                if locked is None or locked.pin() != pin:
                    raise InvalidInputError(
                        "a declared dependency pin is not satisfied by the lock",
                        details={"name": entry.name, "dependency": pin.name},
                    )

    def get(self, name: str) -> LockedCapability | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    def digest(self) -> Digest:
        """A canonical digest over every locked entry's trust-relevant fields.

        Covers the closure digest, origin revision, license, trust
        classification, kind, permissions, compatibility, and dependency
        pins. ``verified_at`` is a timestamp, not trust evidence, and is
        excluded so re-resolving an unchanged package reproduces the digest.
        """

        return Digest.of_json(
            [
                {
                    "name": entry.name,
                    "version": entry.version,
                    "kind": entry.kind.value,
                    "revision": entry.revision,
                    "digest": str(entry.digest),
                    "license": entry.license,
                    "source_label": entry.provenance.source_label,
                    "provenance_status": entry.provenance.status.value,
                    "signer_key_id": entry.provenance.signer_key_id,
                    "introduced_by_run": entry.introduced_by_run,
                    "permissions": [permission.value for permission in entry.permissions],
                    "compatibility": entry.compatibility.to_json(),
                    "dependencies": [pin.to_json() for pin in entry.dependencies],
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
