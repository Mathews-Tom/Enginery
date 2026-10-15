"""Builders for capability package closures, locks, and in-memory sources used by tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import CapabilityDescriptor, PackageFile
from enginery.capabilities.closure import build_closure
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.manifest import (
    CapabilityPermission,
    DependencyPin,
    PackageCompatibility,
    PackageKind,
    PackageManifest,
    render_manifest,
)
from enginery.domain.digests import Digest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def package_files(
    name: str = "skill-a",
    version: str = "1",
    *,
    body: bytes = b"# skill instructions\n",
    extra: Mapping[str, bytes] | None = None,
    license_id: str | None = "MIT",
    kind: PackageKind = PackageKind.INSTRUCTION,
    dependencies: Sequence[DependencyPin] = (),
    permissions: Sequence[CapabilityPermission] = (),
    compatibility: PackageCompatibility | None = None,
) -> tuple[PackageFile, ...]:
    """A complete package: manifest, instruction, reference, plus any ``extra`` files."""

    manifest = PackageManifest(
        name=name,
        version=version,
        kind=kind,
        license=license_id,
        dependencies=tuple(dependencies),
        permissions=tuple(permissions),
        compatibility=compatibility or PackageCompatibility(),
    )
    files = [
        PackageFile("capability.json", render_manifest(manifest)),
        PackageFile("SKILL.md", body),
        PackageFile("references/guide.md", b"reference for " + name.encode()),
    ]
    files.extend(PackageFile(path, content) for path, content in (extra or {}).items())
    return tuple(files)


def replace_file(
    files: Sequence[PackageFile], path: str, content: bytes, *, executable: bool = False
) -> tuple[PackageFile, ...]:
    """``files`` with ``path`` rewritten (or added) to ``content``."""

    kept = [item for item in files if item.path != path]
    kept.append(PackageFile(path, content, executable))
    return tuple(kept)


def closure_digest_of(files: Sequence[PackageFile]) -> Digest:
    return build_closure(files).digest


def pin_for(name: str, files: Sequence[PackageFile], *, version: str = "1") -> DependencyPin:
    return DependencyPin(name=name, version=version, closure_digest=closure_digest_of(files))


def descriptor_for(
    files: Sequence[PackageFile],
    *,
    provenance: str = "repository-local",
    revision: str = "rev-1",
    license_id: str | None = None,
    signature: bytes | None = None,
    signer_key_id: str | None = None,
) -> CapabilityDescriptor:
    closure = build_closure(files)
    return CapabilityDescriptor(
        name=closure.manifest.name,
        version=closure.manifest.version,
        digest=closure.digest,
        provenance=provenance,
        revision=revision,
        license=license_id,
        signature=signature,
        signer_key_id=signer_key_id,
    )


class FakeCapabilitySource:
    """A minimal in-memory ``CapabilitySourcePort`` backed by explicit package file lists."""

    def __init__(
        self,
        descriptors: Mapping[tuple[str, str], CapabilityDescriptor],
        packages: Mapping[tuple[str, str], tuple[PackageFile, ...]],
    ) -> None:
        self._descriptors = dict(descriptors)
        self._packages = dict(packages)

    @classmethod
    def of(
        cls, *packages: Sequence[PackageFile], provenance: str = "repository-local"
    ) -> FakeCapabilitySource:
        descriptors: dict[tuple[str, str], CapabilityDescriptor] = {}
        contents: dict[tuple[str, str], tuple[PackageFile, ...]] = {}
        for files in packages:
            descriptor = descriptor_for(files, provenance=provenance)
            key = (descriptor.name, descriptor.version)
            descriptors[key] = descriptor
            contents[key] = tuple(files)
        return cls(descriptors, contents)

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityDescriptor, ...]:  # pragma: no cover - unused
        return tuple(self._descriptors.values())

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        return self._descriptors.get((name, version))

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        return self._packages[(name, version)]


def locked_entry(
    name: str = "skill-a",
    *,
    files: Sequence[PackageFile] | None = None,
    version: str = "1",
    introduced_by_run: bool = False,
    status: ProvenanceStatus | None = None,
    license_id: str | None = "MIT",
    signer_key_id: str | None = None,
    revision: str = "rev-1",
    body: bytes | None = None,
) -> LockedCapability:
    """A lock entry built from a real closure; trust defaults follow ``introduced_by_run``."""

    if files is None:
        files = package_files(
            name, version, license_id=license_id, body=body or b"# skill instructions\n"
        )
    closure = build_closure(files)
    if status is None:
        status = (
            ProvenanceStatus.UNAUTHENTICATED
            if introduced_by_run
            else ProvenanceStatus.LOCAL_TRUSTED
        )
    if status is ProvenanceStatus.AUTHENTICATED and signer_key_id is None:
        signer_key_id = "key-1"
    return LockedCapability(
        name=closure.manifest.name,
        version=closure.manifest.version,
        kind=closure.manifest.kind,
        revision=revision,
        digest=closure.digest,
        provenance=ProvenanceRecord(
            status=status,
            source_label="test-source",
            signer_key_id=signer_key_id,
            verified_at=NOW,
        ),
        license=license_id,
        introduced_by_run=introduced_by_run,
        files=closure.files,
        dependencies=closure.manifest.dependencies,
        permissions=closure.manifest.permissions,
        compatibility=closure.manifest.compatibility,
    )


def lock_of(*entries: LockedCapability) -> CapabilityLock:
    return CapabilityLock(entries=entries)


__all__ = [
    "NOW",
    "FakeCapabilitySource",
    "closure_digest_of",
    "descriptor_for",
    "lock_of",
    "locked_entry",
    "package_files",
    "pin_for",
    "replace_file",
]
