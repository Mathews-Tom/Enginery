"""Resolve requested capability names into an immutable, classified lock.

The resolver never trusts a source's own claim about itself. It fetches
bytes, verifies them against the source's declared digest (defeating a
digest-swap source), classifies provenance from evidence the caller
supplies rather than the source's self-report, and rejects a resolution
that would silently move an already-locked capability to a new digest
(mutable-reference drift).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.application.delivery_ports import CapabilityDescriptor, CapabilitySourcePort
from enginery.capabilities.errors import (
    CapabilityDigestMismatchError,
    CapabilityLockDriftError,
    CapabilityNotFoundError,
)
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.domain.digests import Digest

# Given a resolved descriptor, return the signer key ID that verified the
# capability's signature, or ``None`` when no signature verified. Injected
# so the resolver stays free of any concrete signature algorithm; the
# default rejects every capability as unsigned.
SignatureVerifier = Callable[[CapabilityDescriptor], str | None]


def _no_signatures(descriptor: CapabilityDescriptor) -> str | None:
    del descriptor
    return None


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """One requested capability name/version pair."""

    name: str
    version: str


class CapabilityResolver:
    """Resolve requested capabilities against ordered sources into a lock.

    ``reviewed_base`` names the exact ``(name, version)`` pairs already
    present in the run's bound, reviewed base revision: those resolve
    without a signature and never require interactive approval, matching
    "existing reviewed local capabilities can resolve by policy." Every
    other resolved entry is ``introduced_by_run`` and reaches
    ``AUTHENTICATED`` provenance only when ``signature_verifier`` reports a
    verified signer; otherwise it is ``UNAUTHENTICATED`` and must be gated
    by ``capability.materialize`` human approval before it can execute.
    """

    def __init__(
        self,
        sources: Sequence[CapabilitySourcePort],
        *,
        reviewed_base: frozenset[tuple[str, str]] = frozenset(),
        signature_verifier: SignatureVerifier = _no_signatures,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not sources:
            raise ValueError("at least one capability source is required")
        self._sources = tuple(sources)
        self._reviewed_base = reviewed_base
        self._signature_verifier = signature_verifier
        self._clock = clock

    def resolve(
        self,
        requested: Iterable[CapabilityRequest],
        *,
        previous_lock: CapabilityLock | None = None,
    ) -> CapabilityLock:
        entries = tuple(
            self._resolve_one(request, previous_lock=previous_lock) for request in requested
        )
        return CapabilityLock(entries=entries)

    def _resolve_one(
        self, request: CapabilityRequest, *, previous_lock: CapabilityLock | None
    ) -> LockedCapability:
        descriptor, source = self._discover(request)
        self._verify_content(descriptor, source)
        self._enforce_no_drift(request, descriptor, previous_lock=previous_lock)
        introduced_by_run = (request.name, request.version) not in self._reviewed_base
        provenance = self._classify_provenance(descriptor, introduced_by_run=introduced_by_run)
        return LockedCapability(
            name=descriptor.name,
            version=descriptor.version,
            digest=descriptor.digest,
            provenance=provenance,
            license=descriptor.license,
            introduced_by_run=introduced_by_run,
        )

    def _discover(
        self, request: CapabilityRequest
    ) -> tuple[CapabilityDescriptor, CapabilitySourcePort]:
        for source in self._sources:
            descriptor = source.resolve(request.name, request.version)
            if descriptor is not None:
                return descriptor, source
        raise CapabilityNotFoundError(
            "capability was not found in any configured source",
            details={"name": request.name, "version": request.version},
        )

    def _verify_content(
        self, descriptor: CapabilityDescriptor, source: CapabilitySourcePort
    ) -> None:
        content = source.fetch(descriptor.name, descriptor.version)
        if Digest.of_bytes(content) != descriptor.digest:
            raise CapabilityDigestMismatchError(
                "fetched capability bytes do not match the resolved digest",
                details={"name": descriptor.name, "version": descriptor.version},
            )

    def _enforce_no_drift(
        self,
        request: CapabilityRequest,
        descriptor: CapabilityDescriptor,
        *,
        previous_lock: CapabilityLock | None,
    ) -> None:
        if previous_lock is None:
            return
        previous_entry = previous_lock.get(request.name)
        if previous_entry is None:
            return
        same_version = previous_entry.version == descriptor.version
        if same_version and previous_entry.digest != descriptor.digest:
            raise CapabilityLockDriftError(
                "a mutable capability reference resolved to a different digest "
                "than the in-flight lock recorded",
                details={"name": request.name, "version": request.version},
            )

    def _classify_provenance(
        self, descriptor: CapabilityDescriptor, *, introduced_by_run: bool
    ) -> ProvenanceRecord:
        if not introduced_by_run:
            return ProvenanceRecord(
                status=ProvenanceStatus.LOCAL_TRUSTED,
                source_label=descriptor.provenance,
                signer_key_id=None,
                verified_at=self._clock(),
            )
        signer_key_id = self._signature_verifier(descriptor)
        if signer_key_id is not None:
            return ProvenanceRecord(
                status=ProvenanceStatus.AUTHENTICATED,
                source_label=descriptor.provenance,
                signer_key_id=signer_key_id,
                verified_at=self._clock(),
            )
        return ProvenanceRecord(
            status=ProvenanceStatus.UNAUTHENTICATED,
            source_label=descriptor.provenance,
            signer_key_id=None,
            verified_at=self._clock(),
        )


__all__ = [
    "CapabilityNotFoundError",
    "CapabilityRequest",
    "CapabilityResolver",
    "SignatureVerifier",
]
