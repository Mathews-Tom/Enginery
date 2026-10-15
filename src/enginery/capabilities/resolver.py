"""Resolve requested capability names into an immutable, classified lock.

The resolver never trusts a source's own claim about itself. It fetches
every file of a package, rebuilds the package closure from those files
(rejecting unsafe paths, symlinks, unreviewed asset types, and limit
breaches), recomputes the closure digest and verifies it against the
source's declared digest (defeating a digest-swap source), resolves each
declared transitive dependency and checks it against its pinned closure
digest, classifies provenance from evidence the caller supplies rather than
the source's self-report, and rejects a resolution that would silently move
an already-locked capability to a new closure (mutable-reference drift).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    CapabilitySourcePort,
    PackageFile,
)
from enginery.capabilities.closure import PackageClosure, build_closure
from enginery.capabilities.errors import (
    CapabilityDependencyError,
    CapabilityDigestMismatchError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
    CapabilityManifestError,
    CapabilityNotFoundError,
)
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)

# Given a resolved descriptor, return the signer key ID that verified the
# capability's signature, or ``None`` when no signature verified. Injected
# so the resolver stays free of any concrete signature algorithm; the
# default rejects every capability as unsigned.
SignatureVerifier = Callable[[CapabilityDescriptor], str | None]

MAX_DEPENDENCY_DEPTH = 8


def _no_signatures(descriptor: CapabilityDescriptor) -> str | None:
    del descriptor
    return None


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """One requested capability name/version pair.

    ``expected_license`` is an optional caller-declared constraint: when
    set, a resolved capability whose effective license differs is rejected
    rather than silently locked under an unexpected license.
    """

    name: str
    version: str
    expected_license: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCapabilities:
    """A resolved lock plus the verified files each entry was locked from.

    ``files_by_name`` is what :func:`enginery.capabilities.materialize.materialize_lock`
    needs; it is exactly the file set whose rebuilt closure matched each
    entry's digest.
    """

    lock: CapabilityLock
    files_by_name: Mapping[str, tuple[PackageFile, ...]]


class CapabilityResolver:
    """Resolve requested capabilities against ordered sources into a lock.

    ``reviewed_base`` names the exact ``(name, version)`` pairs already
    present in the run's bound, reviewed base revision: those resolve
    without a signature and never require interactive approval, matching
    "existing reviewed local capabilities can resolve by policy." Every
    other resolved entry -- including every transitive dependency not in the
    reviewed base -- is ``introduced_by_run`` and reaches ``AUTHENTICATED``
    provenance only when ``signature_verifier`` reports a verified signer;
    otherwise it is ``UNAUTHENTICATED`` and must be gated by
    ``capability.materialize`` human approval before it can execute.
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
        return self.resolve_with_files(requested, previous_lock=previous_lock).lock

    def resolve_with_files(
        self,
        requested: Iterable[CapabilityRequest],
        *,
        previous_lock: CapabilityLock | None = None,
    ) -> ResolvedCapabilities:
        resolved: dict[str, tuple[LockedCapability, tuple[PackageFile, ...]]] = {}
        for request in requested:
            self._resolve_one(request, previous_lock=previous_lock, resolved=resolved, stack=())
        lock = CapabilityLock(entries=tuple(entry for entry, _ in resolved.values()))
        files = {name: package_files for name, (_, package_files) in resolved.items()}
        return ResolvedCapabilities(lock=lock, files_by_name=MappingProxyType(files))

    def _resolve_one(
        self,
        request: CapabilityRequest,
        *,
        previous_lock: CapabilityLock | None,
        resolved: dict[str, tuple[LockedCapability, tuple[PackageFile, ...]]],
        stack: tuple[str, ...],
    ) -> LockedCapability:
        if request.name in stack:
            raise CapabilityDependencyError(
                "capability dependencies are cyclic",
                details={"cycle": [*stack, request.name]},
            )
        if len(stack) >= MAX_DEPENDENCY_DEPTH:
            raise CapabilityDependencyError(
                "capability dependencies are nested too deeply",
                details={"limit": MAX_DEPENDENCY_DEPTH, "name": request.name},
            )
        existing = resolved.get(request.name)
        if existing is not None:
            if existing[0].version != request.version:
                raise CapabilityDependencyError(
                    "two requests resolve the same capability to different versions",
                    details={
                        "name": request.name,
                        "versions": sorted({existing[0].version, request.version}),
                    },
                )
            self._enforce_license(request, existing[0].license)
            return existing[0]
        descriptor, source = self._discover(request)
        files = source.fetch_closure(descriptor.name, descriptor.version)
        closure = self._verify_closure(descriptor, files)
        license_id = self._effective_license(descriptor, closure)
        self._enforce_license(request, license_id)
        self._enforce_no_drift(request, descriptor, previous_lock=previous_lock)
        self._resolve_dependencies(
            closure,
            previous_lock=previous_lock,
            resolved=resolved,
            stack=(*stack, request.name),
        )
        introduced_by_run = (request.name, request.version) not in self._reviewed_base
        entry = LockedCapability(
            name=descriptor.name,
            version=descriptor.version,
            kind=closure.manifest.kind,
            revision=descriptor.revision,
            digest=closure.digest,
            provenance=self._classify_provenance(descriptor, introduced_by_run=introduced_by_run),
            license=license_id,
            introduced_by_run=introduced_by_run,
            files=closure.files,
            dependencies=closure.manifest.dependencies,
            permissions=closure.manifest.permissions,
            compatibility=closure.manifest.compatibility,
        )
        resolved[entry.name] = (entry, tuple(files))
        return entry

    def _resolve_dependencies(
        self,
        closure: PackageClosure,
        *,
        previous_lock: CapabilityLock | None,
        resolved: dict[str, tuple[LockedCapability, tuple[PackageFile, ...]]],
        stack: tuple[str, ...],
    ) -> None:
        for pin in closure.manifest.dependencies:
            dependency = self._resolve_one(
                CapabilityRequest(name=pin.name, version=pin.version),
                previous_lock=previous_lock,
                resolved=resolved,
                stack=stack,
            )
            if dependency.digest != pin.closure_digest:
                raise CapabilityDependencyError(
                    "a dependency resolved to a closure other than its pinned digest",
                    details={
                        "package": stack[-1],
                        "dependency": pin.name,
                        "version": pin.version,
                        "pinned": str(pin.closure_digest),
                        "resolved": str(dependency.digest),
                    },
                )

    def _verify_closure(
        self, descriptor: CapabilityDescriptor, files: Sequence[PackageFile]
    ) -> PackageClosure:
        closure = build_closure(files)
        if closure.digest != descriptor.digest:
            raise CapabilityDigestMismatchError(
                "fetched package files do not match the resolved closure digest",
                details={"name": descriptor.name, "version": descriptor.version},
            )
        manifest = closure.manifest
        if (manifest.name, manifest.version) != (descriptor.name, descriptor.version):
            raise CapabilityManifestError(
                "package manifest identity does not match the requested capability",
                details={
                    "requested": f"{descriptor.name}@{descriptor.version}",
                    "manifest": f"{manifest.name}@{manifest.version}",
                },
            )
        return closure

    def _effective_license(
        self, descriptor: CapabilityDescriptor, closure: PackageClosure
    ) -> str | None:
        """The package's own declared license, else the source's declared metadata.

        The manifest is covered by the closure digest, so it is the
        authoritative declaration; a source may only fill a gap, never
        contradict it.
        """

        declared = closure.manifest.license
        if declared is not None and descriptor.license not in (None, declared):
            raise CapabilityManifestError(
                "source license metadata contradicts the package manifest license",
                details={
                    "name": descriptor.name,
                    "manifest_license": declared,
                    "source_license": descriptor.license,
                },
            )
        return declared if declared is not None else descriptor.license

    def _enforce_license(self, request: CapabilityRequest, license_id: str | None) -> None:
        if request.expected_license is None:
            return
        if license_id != request.expected_license:
            raise CapabilityLicenseMismatchError(
                "resolved capability license does not match the expected license",
                details={
                    "name": request.name,
                    "version": request.version,
                    "expected_license": request.expected_license,
                    "actual_license": license_id,
                },
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
                "a mutable capability reference resolved to a different closure digest "
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
    "MAX_DEPENDENCY_DEPTH",
    "CapabilityNotFoundError",
    "CapabilityRequest",
    "CapabilityResolver",
    "ResolvedCapabilities",
    "SignatureVerifier",
]
