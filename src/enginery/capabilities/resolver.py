"""Resolve requested capability names into an immutable, classified lock.

The resolver never trusts a source's own claim about itself. It selects the
highest-precedence typed source offering a package (see
``enginery.capabilities.sources``), fetches every file of the package,
rebuilds the package closure from those files (rejecting unsafe paths,
symlinks, unreviewed asset types, and limit breaches), recomputes the closure
digest and verifies it against the source's declared digest (defeating a
digest-swap source), resolves each declared transitive dependency and checks
it against its pinned closure digest, classifies provenance from the
source's kind and the evidence the caller supplies rather than the source's
self-report, records a host/API compatibility receipt, and rejects a
resolution that would silently move an already-locked capability to a new
closure (mutable-reference drift).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from enginery.application.delivery_ports import CapabilityDescriptor, PackageFile
from enginery.capabilities.closure import (
    PackageClosure,
    build_closure,
    is_license_like_name,
    is_notice_file_name,
)
from enginery.capabilities.compatibility import HostProfile, current_host, evaluate_compatibility
from enginery.capabilities.errors import (
    CapabilityDependencyError,
    CapabilityDigestMismatchError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
    CapabilityManifestError,
    CapabilityNotFoundError,
)
from enginery.capabilities.license_text import (
    UNKNOWN_LICENSE,
    classify_license_text,
    notice_conflicts,
)
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.sources import (
    CapabilitySource,
    SourceCandidate,
    SourceSelection,
    order_sources,
)
from enginery.domain.errors import EngineryError

# Given a resolved descriptor, return the signer key ID that verified the
# capability's signature, or ``None`` when no signature verified. Injected
# so the resolver stays free of any concrete signature algorithm; the
# default rejects every capability as unsigned.
SignatureVerifier = Callable[[CapabilityDescriptor], str | None]


def _decode(item: PackageFile) -> str:
    return item.content.decode("utf-8", errors="replace")


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
    """A resolved lock plus the verified files and source selection of each entry.

    ``files_by_name`` is what :func:`enginery.capabilities.materialize.materialize_lock`
    needs; it is exactly the file set whose rebuilt closure matched each
    entry's digest. ``selections`` records, per package, which source was
    selected and (when requested) every shadowed lower-precedence alternative.
    """

    lock: CapabilityLock
    files_by_name: Mapping[str, tuple[PackageFile, ...]]
    selections: Mapping[str, SourceSelection]


@dataclass(frozen=True, slots=True)
class _Resolved:
    entry: LockedCapability
    files: tuple[PackageFile, ...]
    selection: SourceSelection


class CapabilityResolver:
    """Resolve requested capabilities against typed sources into a lock.

    Only the ``project`` source (packages in the run's bound, reviewed base
    revision) resolves without a signature and never requires interactive
    approval, matching "existing reviewed local capabilities can resolve by
    policy." Every other resolved entry -- including every transitive
    dependency that is not served by the project source -- is
    ``introduced_by_run`` and reaches ``AUTHENTICATED`` provenance only when
    ``signature_verifier`` reports a verified signer; otherwise it is
    ``UNAUTHENTICATED`` and must be gated by ``capability.materialize``
    human approval before it can execute.
    """

    def __init__(
        self,
        sources: Sequence[CapabilitySource],
        *,
        signature_verifier: SignatureVerifier = _no_signatures,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        host: HostProfile | None = None,
    ) -> None:
        if not sources:
            raise ValueError("at least one capability source is required")
        self._sources = order_sources(sources)
        self._signature_verifier = signature_verifier
        self._clock = clock
        self._host = host or current_host()

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
        report_shadowed: bool = False,
    ) -> ResolvedCapabilities:
        """Resolve ``requested``; ``report_shadowed`` also queries every lower-precedence source."""

        resolved: dict[str, _Resolved] = {}
        for request in requested:
            self._resolve_one(
                request,
                previous_lock=previous_lock,
                resolved=resolved,
                stack=(),
                report_shadowed=report_shadowed,
            )
        lock = CapabilityLock(entries=tuple(item.entry for item in resolved.values()))
        return ResolvedCapabilities(
            lock=lock,
            files_by_name=MappingProxyType({name: item.files for name, item in resolved.items()}),
            selections=MappingProxyType({name: item.selection for name, item in resolved.items()}),
        )

    def _resolve_one(
        self,
        request: CapabilityRequest,
        *,
        previous_lock: CapabilityLock | None,
        resolved: dict[str, _Resolved],
        stack: tuple[str, ...],
        report_shadowed: bool,
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
            if existing.entry.version != request.version:
                raise CapabilityDependencyError(
                    "two requests resolve the same capability to different versions",
                    details={
                        "name": request.name,
                        "versions": sorted({existing.entry.version, request.version}),
                    },
                )
            self._enforce_license(request, existing.entry.license)
            return existing.entry
        source, descriptor, selection = self._select(request, report_shadowed=report_shadowed)
        files = tuple(source.port.fetch_closure(descriptor.name, descriptor.version))
        closure = self._verify_closure(descriptor, files)
        license_id = self._effective_license(descriptor, closure, files)
        self._enforce_license(request, license_id)
        self._enforce_no_drift(request, descriptor, previous_lock=previous_lock)
        self._resolve_dependencies(
            closure,
            previous_lock=previous_lock,
            resolved=resolved,
            stack=(*stack, request.name),
            report_shadowed=report_shadowed,
        )
        introduced_by_run = not source.reviewed_base
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
            receipt=evaluate_compatibility(closure.manifest.compatibility, self._host),
            dependencies=closure.manifest.dependencies,
            permissions=closure.manifest.permissions,
            compatibility=closure.manifest.compatibility,
        )
        resolved[entry.name] = _Resolved(entry=entry, files=tuple(files), selection=selection)
        return entry

    def _resolve_dependencies(
        self,
        closure: PackageClosure,
        *,
        previous_lock: CapabilityLock | None,
        resolved: dict[str, _Resolved],
        stack: tuple[str, ...],
        report_shadowed: bool,
    ) -> None:
        for pin in closure.manifest.dependencies:
            dependency = self._resolve_one(
                CapabilityRequest(name=pin.name, version=pin.version),
                previous_lock=previous_lock,
                resolved=resolved,
                stack=stack,
                report_shadowed=report_shadowed,
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

    def _select(
        self, request: CapabilityRequest, *, report_shadowed: bool
    ) -> tuple[CapabilitySource, CapabilityDescriptor, SourceSelection]:
        """The highest-precedence source offering the package, and any shadowed offers.

        A selected source that cannot lock its package fails loudly. A shadowed
        source that cannot is reported with its problem instead of hiding.
        """

        chosen: CapabilitySource | None = None
        selected: SourceCandidate | None = None
        chosen_descriptor: CapabilityDescriptor | None = None
        shadowed: list[SourceCandidate] = []
        for source in self._sources:
            if chosen is not None and not report_shadowed:
                break
            if chosen is None:
                descriptor = source.port.resolve(request.name, request.version)
                if descriptor is None:
                    continue
                chosen, chosen_descriptor = source, descriptor
                selected = SourceCandidate(
                    kind=source.kind, label=source.label, descriptor=descriptor
                )
                continue
            try:
                alternative = source.port.resolve(request.name, request.version)
            except EngineryError as error:
                shadowed.append(
                    SourceCandidate(kind=source.kind, label=source.label, problem=str(error))
                )
                continue
            if alternative is not None:
                shadowed.append(
                    SourceCandidate(kind=source.kind, label=source.label, descriptor=alternative)
                )
        if chosen is None or selected is None or chosen_descriptor is None:
            raise CapabilityNotFoundError(
                "capability was not found in any configured source",
                details={"name": request.name, "version": request.version},
            )
        selection = SourceSelection(selected=selected, shadowed=tuple(shadowed))
        return chosen, chosen_descriptor, selection

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
        self,
        descriptor: CapabilityDescriptor,
        closure: PackageClosure,
        files: Sequence[PackageFile],
    ) -> str | None:
        """The package's declared license, kept only while its shipped license files agree.

        The manifest is covered by the closure digest, so it is the
        authoritative declaration; a source may only fill a gap, never
        contradict it. When the closure ships root license-like files, every
        one must classify to that same SPDX id and no ``NOTICE`` may conflict;
        otherwise the license is ``unknown``. A package without such files
        keeps the declared license.
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
        license_id = declared if declared is not None else descriptor.license
        if license_id is None:
            return None
        shipped = [item for item in files if is_license_like_name(item.path)]
        notices = [item for item in shipped if is_notice_file_name(item.path)]
        if any(notice_conflicts(_decode(item)) for item in notices):
            return UNKNOWN_LICENSE
        deciding = [item for item in shipped if item not in notices]
        if not deciding:
            return license_id
        found = {classify_license_text(_decode(item)) for item in deciding}
        return license_id if found == {license_id} else UNKNOWN_LICENSE

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
