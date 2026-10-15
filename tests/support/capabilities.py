"""Builders for capability package closures, locks, and in-memory sources used by tests."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from enginery.adapters.git_capability import GitCapabilitySource
from enginery.application.adapter_types import ADAPTER_API_VERSION, AdapterStatus
from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    CapabilityListing,
    CapabilitySourcePort,
    PackageFile,
)
from enginery.capabilities.closure import build_closure
from enginery.capabilities.compatibility import HostProfile, evaluate_compatibility
from enginery.capabilities.lock import (
    ApprovedCapabilities,
    CapabilityApproval,
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.manifest import (
    CapabilityPermission,
    CommandSpec,
    DependencyPin,
    PackageCompatibility,
    PackageKind,
    PackageManifest,
    render_manifest,
)
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.domain.digests import Digest

NOW = datetime(2026, 1, 1, tzinfo=UTC)
HOST = HostProfile(adapter_api_version=ADAPTER_API_VERSION, platform="linux")
DEFAULT_COMMAND = CommandSpec.create(
    argv=("python3", "scripts/run.py"), input_schema={"type": "object"}
)
DEFAULT_SCRIPT = (
    b"import json, sys\njson.dump({'echo': json.load(sys.stdin)}, sys.stdout, sort_keys=True)\n"
)


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
    command: CommandSpec | None = None,
    script: bytes = DEFAULT_SCRIPT,
) -> tuple[PackageFile, ...]:
    """A complete package: manifest, instruction, reference, plus any ``extra`` files.

    A command or hook package also gets a ``command`` (default: run
    ``scripts/run.py`` through ``python3``), the ``process.execute`` permission
    it must request, and the script file itself.
    """

    runnable = kind in (PackageKind.COMMAND, PackageKind.HOOK)
    if runnable and command is None:
        command = DEFAULT_COMMAND
    declared = tuple(permissions)
    if runnable and CapabilityPermission.PROCESS_EXECUTE not in declared:
        declared = (*declared, CapabilityPermission.PROCESS_EXECUTE)
    manifest = PackageManifest(
        name=name,
        version=version,
        kind=kind,
        license=license_id,
        dependencies=tuple(dependencies),
        permissions=declared,
        compatibility=compatibility or PackageCompatibility(),
        command=command,
    )
    files = [
        PackageFile("capability.json", render_manifest(manifest)),
        PackageFile("SKILL.md", body),
        PackageFile("references/guide.md", b"reference for " + name.encode()),
    ]
    if runnable and command is not None:
        files.append(PackageFile(command.script, script, executable=command.interpreter is None))
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


PINNED_COMMIT = "0123456789abcdef0123456789abcdef01234567"


class FakeCapabilitySource:
    """A minimal in-memory ``CapabilitySourcePort`` backed by explicit package file lists."""

    def __init__(
        self,
        descriptors: Mapping[tuple[str, str], CapabilityDescriptor],
        packages: Mapping[tuple[str, str], tuple[PackageFile, ...]],
        *,
        pinned_commit: str | None = None,
    ) -> None:
        self._descriptors = dict(descriptors)
        self._packages = dict(packages)
        # A project-kind source must be pinned to a commit; the fake only declares one.
        self.pinned_commit = pinned_commit

    @classmethod
    def of(
        cls,
        *packages: Sequence[PackageFile],
        provenance: str = "repository-local",
        revision: str = "rev-1",
        pinned_commit: str | None = None,
    ) -> FakeCapabilitySource:
        descriptors: dict[tuple[str, str], CapabilityDescriptor] = {}
        contents: dict[tuple[str, str], tuple[PackageFile, ...]] = {}
        for files in packages:
            descriptor = descriptor_for(files, provenance=provenance, revision=revision)
            key = (descriptor.name, descriptor.version)
            descriptors[key] = descriptor
            contents[key] = tuple(files)
        return cls(descriptors, contents, pinned_commit=pinned_commit)

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityListing, ...]:  # pragma: no cover - unused
        return tuple(
            CapabilityListing(
                name=item.name,
                version=item.version,
                provenance=item.provenance,
                revision=item.revision,
            )
            for item in self._descriptors.values()
        )

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
        receipt=evaluate_compatibility(closure.manifest.compatibility, HOST),
        dependencies=closure.manifest.dependencies,
        permissions=closure.manifest.permissions,
        compatibility=closure.manifest.compatibility,
    )


def lock_of(*entries: LockedCapability) -> CapabilityLock:
    return CapabilityLock(entries=entries)


class FakeProjectSource(GitCapabilitySource):
    """A project-kind port serving in-memory packages; git is never run.

    It is a real :class:`GitCapabilitySource` (the resolver accepts no other kind of
    project source) whose port methods are answered by a fake, so no repository is read.
    """

    def __init__(self, inner: FakeCapabilitySource, *, revision: str = PINNED_COMMIT) -> None:
        super().__init__(repository=tempfile.gettempdir(), revision=revision)
        self._inner = inner

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        return self._inner.probe()

    def discover(self) -> tuple[CapabilityListing, ...]:
        return self._inner.discover()

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        return self._inner.resolve(name, version)

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        return self._inner.fetch_closure(name, version)


def capability_source(
    kind: SourceKind, *packages: Sequence[PackageFile], label: str | None = None
) -> CapabilitySource:
    """A typed source wrapping an in-memory fake that serves ``packages``."""

    port: CapabilitySourcePort
    if kind is SourceKind.PROJECT:
        port = FakeProjectSource(
            FakeCapabilitySource.of(*packages, provenance=kind.value, revision=PINNED_COMMIT)
        )
    else:
        port = FakeCapabilitySource.of(*packages, provenance=kind.value)
    return CapabilitySource(kind=kind, label=label or f"{kind.value}-source", port=port)


def project_source(*packages: Sequence[PackageFile]) -> CapabilitySource:
    """A reviewed-base source: its packages resolve as ``local_trusted``."""

    return capability_source(SourceKind.PROJECT, *packages)


def user_source(*packages: Sequence[PackageFile]) -> CapabilitySource:
    """A user-local source: its packages are run-introduced and unauthenticated."""

    return capability_source(SourceKind.USER, *packages)


def wrap_source(port: CapabilitySourcePort, kind: SourceKind = SourceKind.USER) -> CapabilitySource:
    return CapabilitySource(kind=kind, label=f"{kind.value}-source", port=port)


def approvals_for(lock: CapabilityLock, names: Iterable[str]) -> ApprovedCapabilities:
    """Digest-bound approvals for the named entries of ``lock``, bypassing policy (tests only)."""

    lock_digest = lock.digest()
    chosen = frozenset(names)
    return ApprovedCapabilities(
        frozenset(
            CapabilityApproval(entry.name, entry.digest, lock_digest)
            for entry in lock.entries
            if entry.name in chosen
        )
    )


__all__ = [
    "HOST",
    "NOW",
    "PINNED_COMMIT",
    "FakeCapabilitySource",
    "FakeProjectSource",
    "approvals_for",
    "capability_source",
    "closure_digest_of",
    "descriptor_for",
    "lock_of",
    "locked_entry",
    "package_files",
    "pin_for",
    "project_source",
    "replace_file",
    "user_source",
    "wrap_source",
]
