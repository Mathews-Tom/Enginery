"""Execute generated supply-chain and mutation attacks against capability locking.

Covers an unsigned external asset, a wrong signer, a digest swap, a license
mismatch, a run-introduced capability without approval, and an active-run
mutation (mutable-reference drift against an in-flight lock), plus the
closure cases: a changed sibling script, reference, or dependency pin, a
symlink escape, path traversal, an unreviewed asset type, an unknown-license
redistribution hold, an unauthenticated asset without approval, an approval
superseded after drift, and an uninstall that retains evidence and the
ledger. Every case builds a complete package closure and attacks the
resolver, lock, materializer, install, or uninstall around it.

A hostile closure is served under a digest that vouches for it (a directory
source with the file on disk, or a source declaring the digest of what it
serves), so the digest check cannot be what refuses it. Those cases approve
everything the lock holds, attempt the install, and require both the expected
refusal and that nothing was written. Approvals are digest-bound: a case that
needs one builds it for the exact lock, and the unauthenticated and superseded
cases also present approvals computed for a different lock.

Each guard a case exercises has a mutation test
(``tests/governance/test_capability_gate.py``) proving the case fails, with
that case's own message, when the guard is removed. The two retention rows do
not remove a library guard: they inject a destructive uninstall to prove the
retention assertions can fail.
"""

from __future__ import annotations

import random
import secrets
import string
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from enginery.adapters.directory_capability import DirectoryCapabilitySource
from enginery.adapters.git_capability import GitCapabilitySource
from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import CapabilityDescriptor, CapabilityListing, PackageFile
from enginery.capabilities import approval as approval_module
from enginery.capabilities import install as install_module
from enginery.capabilities.closure import AssetRecord, build_closure, classify_asset, closure_digest
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityAssetTypeError,
    CapabilityDependencyError,
    CapabilityDigestMismatchError,
    CapabilityInstallConflictError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
    CapabilityNotInstalledError,
    CapabilityPathError,
    CapabilitySymlinkError,
)
from enginery.capabilities.install import (
    REVOKED_RECORD_PREFIX,
    install_lock,
    load_owned_manifest,
    uninstall_install,
)
from enginery.capabilities.lock import (
    NO_APPROVALS,
    ApprovedCapabilities,
    CapabilityLock,
    ProvenanceStatus,
)
from enginery.capabilities.manifest import (
    DependencyPin,
    PackageKind,
    PackageManifest,
    render_manifest,
)
from enginery.capabilities.materialize import digest_path, materialize_lock, verify_materialized
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.serialization import write_lock
from enginery.capabilities.signature import PinnedKeyring, PinnedPublisherKey, descriptor_verifier
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.domain.digests import Digest
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator


def _token(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(20))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _expect(exception_type: type[Exception], operation: Callable[[], object]) -> None:
    try:
        operation()
    except exception_type:
        return
    except Exception as error:
        raise RuntimeError(
            f"expected {exception_type.__name__} but got {type(error).__name__}: {error}"
        ) from error
    raise RuntimeError(f"expected {exception_type.__name__} was not raised")


def _package(
    name: str,
    payload: bytes,
    *,
    license_id: str | None = "MIT",
    extra: Mapping[str, bytes] | None = None,
    dependencies: Sequence[DependencyPin] = (),
) -> tuple[PackageFile, ...]:
    manifest = PackageManifest(
        name=name,
        version="1",
        kind=PackageKind.INSTRUCTION,
        license=license_id,
        dependencies=tuple(dependencies),
    )
    files = [
        PackageFile("capability.json", render_manifest(manifest)),
        PackageFile("SKILL.md", payload),
        PackageFile("references/notes.md", payload + b"-reference"),
    ]
    files.extend(PackageFile(path, content) for path, content in (extra or {}).items())
    return tuple(files)


def _descriptor(
    name: str,
    files: tuple[PackageFile, ...],
    *,
    provenance: str = "armory",
    license_id: str | None = None,
    signature: bytes | None = None,
    signer_key_id: str | None = None,
    revision: str = "adversarial-revision",
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        version="1",
        digest=build_closure(files).digest,
        provenance=provenance,
        revision=revision,
        license=license_id,
        signature=signature,
        signer_key_id=signer_key_id,
    )


_PINNED_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_OPERATOR = AuthorityPrincipal(
    id="gate-operator",
    principal_type=PrincipalType.HUMAN,
    role="operator",
    authorization_source="cli",
)
_RUN = AuthorityPrincipal(
    id="gate-run",
    principal_type=PrincipalType.AGENT,
    role="run",
    authorization_source="coordinator",
)


class _FakeSource(GitCapabilitySource):
    """An in-memory stand-in for any source kind, answering the port from memory.

    The resolver accepts only a Git source as the project source, so this is one; no
    repository is ever read.
    """

    def __init__(self, descriptor: CapabilityDescriptor, files: tuple[PackageFile, ...]) -> None:
        super().__init__(repository=tempfile.gettempdir(), revision=_PINNED_COMMIT)
        self._descriptor = descriptor
        self._files = files

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityListing, ...]:  # pragma: no cover - unused
        return (
            CapabilityListing(
                name=self._descriptor.name,
                version=self._descriptor.version,
                provenance=self._descriptor.provenance,
                revision=self._descriptor.revision,
            ),
        )

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        if (name, version) == (self._descriptor.name, self._descriptor.version):
            return self._descriptor
        return None

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        del name, version
        return self._files


def _source(
    descriptor: CapabilityDescriptor,
    files: tuple[PackageFile, ...],
    kind: SourceKind = SourceKind.USER,
) -> CapabilitySource:
    port = _FakeSource(descriptor, files)
    return CapabilitySource(kind=kind, label=f"{kind.value}-gate", port=port)


class _MultiSource(GitCapabilitySource):
    """An in-memory source serving several packages, each with its own descriptor and files.

    It is a committed-tree source pinned to one commit, so it may be registered as the project
    source; no repository is ever read.
    """

    def __init__(self, *packages: tuple[CapabilityDescriptor, tuple[PackageFile, ...]]) -> None:
        super().__init__(repository=tempfile.gettempdir(), revision=_PINNED_COMMIT)
        self._served_packages = {(item[0].name, item[0].version): item for item in packages}

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityListing, ...]:  # pragma: no cover - unused
        return tuple(
            CapabilityListing(
                name=descriptor.name,
                version=descriptor.version,
                provenance=descriptor.provenance,
                revision=descriptor.revision,
            )
            for descriptor, _ in self._served_packages.values()
        )

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        package = self._served_packages.get((name, version))
        return None if package is None else package[0]

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        return self._served_packages[(name, version)][1]


def _served(kind: SourceKind, *packages: tuple[PackageFile, ...]) -> CapabilitySource:
    """A typed in-memory source serving each of ``packages`` under their own manifest identity."""

    served = tuple(
        (
            _descriptor(
                build_closure(files).manifest.name,
                files,
                provenance=kind.value,
                revision=_PINNED_COMMIT,
            ),
            files,
        )
        for files in packages
    )
    return CapabilitySource(kind=kind, label=f"{kind.value}-gate", port=_MultiSource(*served))


def _directory_source(root: Path, kind: SourceKind) -> CapabilitySource:
    port = DirectoryCapabilitySource(root, provenance=kind.value)
    return CapabilitySource(kind=kind, label=f"{kind.value}-gate", port=port)


def _request(name: str) -> CapabilityRequest:
    return CapabilityRequest(name=name, version="1")


def _replace(files: tuple[PackageFile, ...], path: str, content: bytes) -> tuple[PackageFile, ...]:
    _assert(any(item.path == path for item in files), f"{path} is not part of the package")
    return tuple(
        PackageFile(item.path, content, item.executable) if item.path == path else item
        for item in files
    )


def _write_directory(root: Path, name: str, files: Sequence[PackageFile]) -> Path:
    directory = root / name
    for item in files:
        target = directory / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)
        if item.executable:
            target.chmod(0o755)
    return directory


def _case_unsigned_external_asset(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode())
    resolver = CapabilityResolver([_source(_descriptor(name, files), files)])

    lock = resolver.resolve([CapabilityRequest(name=name, version="1")])

    entry = lock.get(name)
    _assert(entry is not None, "resolution silently dropped an unsigned capability")
    assert entry is not None
    _assert(
        entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED,
        "an unsigned external asset must not classify as authenticated",
    )
    _expect(
        CapabilityApprovalRequiredError,
        lambda: materialize_lock(lock, {name: files}, root=_scratch_root()),
    )


def _case_wrong_signer(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode())
    digest = build_closure(files).digest
    actual_signer = Ed25519PrivateKey.generate()
    claimed_signer_key = Ed25519PrivateKey.generate()
    signature = actual_signer.sign(digest.hex_value.encode("ascii"))
    descriptor = _descriptor(
        name,
        files,
        provenance="external-registry",
        signature=signature,
        signer_key_id="claimed-signer",
    )
    keyring = PinnedKeyring([PinnedPublisherKey("claimed-signer", claimed_signer_key.public_key())])
    resolver = CapabilityResolver(
        [_source(descriptor, files)], signature_verifier=descriptor_verifier(keyring)
    )

    lock = resolver.resolve([CapabilityRequest(name=name, version="1")])

    entry = lock.get(name)
    assert entry is not None
    _assert(
        entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED,
        "a signature from the wrong signer must not verify as authenticated",
    )


def _case_digest_swap(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    declared_files = _package(name, _token(rng).encode())
    swapped_files = _package(name, _token(rng).encode())
    resolver = CapabilityResolver([_source(_descriptor(name, declared_files), swapped_files)])

    _expect(
        CapabilityDigestMismatchError,
        lambda: resolver.resolve([CapabilityRequest(name=name, version="1")]),
    )


def _case_license_mismatch(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode(), license_id=None)
    descriptor = _descriptor(name, files, license_id="GPL-3.0")
    resolver = CapabilityResolver([_source(descriptor, files)])

    _expect(
        CapabilityLicenseMismatchError,
        lambda: resolver.resolve(
            [CapabilityRequest(name=name, version="1", expected_license="MIT")]
        ),
    )


def _case_run_introduced_capability_blocks_without_approval(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode())
    resolver = CapabilityResolver([_source(_descriptor(name, files), files)])

    lock = resolver.resolve([CapabilityRequest(name=name, version="1")])
    entry = lock.get(name)
    assert entry is not None
    _assert(
        entry.introduced_by_run is True,
        "capability outside the reviewed base must be run-introduced",
    )

    _expect(
        CapabilityApprovalRequiredError,
        lambda: materialize_lock(lock, {name: files}, root=_scratch_root()),
    )
    materialized = materialize_lock(
        lock,
        {name: files},
        root=_scratch_root(),
        approvals=_approve(lock, [name]),
    )
    _assert(name in materialized, "an approved run-introduced capability must materialize")


def _case_active_run_mutation(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    original_files = _package(name, _token(rng).encode())
    mutated_files = _package(name, _token(rng).encode())
    original = _descriptor(
        name, original_files, provenance="repository-local", revision=_PINNED_COMMIT
    )
    mutated = _descriptor(
        name, mutated_files, provenance="repository-local", revision=_PINNED_COMMIT
    )
    resolver = CapabilityResolver([_source(original, original_files, SourceKind.PROJECT)])
    previous_lock: CapabilityLock = resolver.resolve([CapabilityRequest(name=name, version="1")])

    mutated_resolver = CapabilityResolver([_source(mutated, mutated_files, SourceKind.PROJECT)])

    _expect(
        CapabilityLockDriftError,
        lambda: mutated_resolver.resolve(
            [CapabilityRequest(name=name, version="1")], previous_lock=previous_lock
        ),
    )


def _approve(lock: CapabilityLock, names: list[str]) -> ApprovedCapabilities:
    """Approvals through the real path: a registered human records the exact schema."""

    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("policy-gate", approval_registry=registry)
    for entry in lock.pending_approval():
        if entry.name in names:
            schema = approval_module.materialize_approval_schema(
                entry, lock, requesting_principal_id=_RUN.id
            )
            registry.record_approval(schema, [_OPERATOR])
    return approval_module.approved_capabilities(lock, evaluator, requesting_principal_id=_RUN.id)


def _case_sibling_script_drift(rng: random.Random) -> None:
    """A changed sibling script changes the lock digest and is drift against a prior lock."""

    name = f"skill-{_token(rng)}"
    payload = _token(rng).encode()
    reviewed = _package(name, payload, extra={"scripts/helper.py": b"print('reviewed')\n"})
    drifted = _replace(reviewed, "scripts/helper.py", b"print('changed after review')\n")
    original = CapabilityResolver([_served(SourceKind.PROJECT, reviewed)])
    changed = CapabilityResolver([_served(SourceKind.PROJECT, drifted)])

    previous = original.resolve([_request(name)])
    current = changed.resolve([_request(name)])

    _assert(
        previous.digest() != current.digest(),
        "a sibling script change must change the lock digest",
    )
    _expect(
        CapabilityLockDriftError,
        lambda: changed.resolve([_request(name)], previous_lock=previous),
    )


def _case_reference_drift(rng: random.Random) -> None:
    """A reference changed in the materialized store is rejected on every later use."""

    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode())
    with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
        base = Path(scratch)
        _write_directory(base / "packages", name, files)
        resolved = CapabilityResolver(
            [_directory_source(base / "packages", SourceKind.USER)]
        ).resolve_with_files([_request(name)])
        approvals = _approve(resolved.lock, [name])
        install_root, store = base / "installed", base / "store"
        install_lock(
            resolved, install_root=install_root, capabilities_root=store, approvals=approvals
        )
        entry = resolved.lock.entries[0]
        reference = digest_path(store, entry.digest) / "references" / "notes.md"
        reference.chmod(0o644)
        reference.write_bytes(b"edited after the closure was locked")

        _expect(CapabilityIntegrityError, lambda: verify_materialized(entry, store))
        _expect(
            CapabilityIntegrityError,
            lambda: install_lock(
                resolved, install_root=install_root, capabilities_root=store, approvals=approvals
            ),
        )


def _case_dependency_pin_drift(rng: random.Random) -> None:
    """A dependency that no longer matches its pinned closure digest fails resolution."""

    parent, dependency = f"skill-{_token(rng)}", f"dep-{_token(rng)}"
    payload = _token(rng).encode()
    dependency_files = _package(
        dependency, _token(rng).encode(), extra={"scripts/check.py": b"print('reviewed')\n"}
    )
    changed_dependency = _replace(dependency_files, "scripts/check.py", b"print('changed')\n")
    pin = DependencyPin(dependency, "1", build_closure(dependency_files).digest)
    repin = DependencyPin(dependency, "1", build_closure(changed_dependency).digest)
    parent_files = _package(parent, payload, dependencies=(pin,))
    repinned_files = _package(parent, payload, dependencies=(repin,))

    honest = CapabilityResolver([_served(SourceKind.PROJECT, parent_files, dependency_files)])
    lock = honest.resolve([_request(parent)])
    _assert(
        lock.get(dependency) is not None,
        "a declared dependency must be locked as an entry of its own",
    )
    _assert(
        build_closure(parent_files).digest != build_closure(repinned_files).digest,
        "a dependency pin change must change the parent closure digest",
    )
    tampered = CapabilityResolver([_served(SourceKind.PROJECT, parent_files, changed_dependency)])

    _expect(CapabilityDependencyError, lambda: tampered.resolve([_request(parent)]))


def _case_symlink_escape(rng: random.Random) -> None:
    """A symlink cannot pull outside content into a closure or redirect an install."""

    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode())
    with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
        base = Path(scratch)
        secret = base / "secret.md"
        secret.write_bytes(b"content outside the package")
        directory = _write_directory(base / "packages", name, files)
        (directory / "references" / "escape.md").symlink_to(secret)
        source = DirectoryCapabilitySource(base / "packages", provenance="user-directory")

        _expect(CapabilitySymlinkError, lambda: source.resolve(name, "1"))
        _expect(CapabilitySymlinkError, lambda: source.fetch_closure(name, "1"))

        _write_directory(base / "clean", name, files)
        resolved = CapabilityResolver(
            [_directory_source(base / "clean", SourceKind.USER)]
        ).resolve_with_files([_request(name)])
        outside, install_root = base / "outside", base / "installed"
        outside.mkdir()
        install_root.mkdir()
        (install_root / name).symlink_to(outside, target_is_directory=True)

        _expect(
            CapabilityInstallConflictError,
            lambda: install_lock(
                resolved,
                install_root=install_root,
                capabilities_root=base / "store",
                approvals=_approve(resolved.lock, [name]),
            ),
        )
        _assert(not any(outside.iterdir()), "an install wrote through a symlinked destination")


def _resolve_and_install(source: CapabilitySource, name: str, work: Path) -> None:
    """Resolve ``name`` from ``source`` and install it, approving everything the lock holds.

    With every approval granted, only the closure guards can stop the attack, not the
    approval requirement.
    """

    resolved = CapabilityResolver([source]).resolve_with_files([_request(name)])
    approvals = _approve(resolved.lock, [entry.name for entry in resolved.lock.entries])
    install_lock(
        resolved,
        install_root=work / "installed",
        capabilities_root=work / "store",
        approvals=approvals,
    )


def _refused(
    error_type: type[Exception], attack: Callable[[], object], *, written: Path, what: str
) -> None:
    """``attack`` must raise ``error_type`` and must leave nothing under ``written``.

    The filesystem is checked before the exception, so with the guard removed a hostile closure
    that was resolved and installed is reported as what it did, not as a missing exception.
    """

    raised: Exception | None = None
    try:
        attack()
    except Exception as error:
        raised = error
    leftovers = _written_files(written)
    _assert(not leftovers, f"{what} reached the filesystem: {leftovers[:3]}")
    if raised is None:
        raise RuntimeError(f"{what} was not refused; expected {error_type.__name__}")
    if not isinstance(raised, error_type):
        raise RuntimeError(
            f"{what} was refused by {type(raised).__name__}, "
            f"expected {error_type.__name__}: {raised}"
        )


def _closure_digest_including(clean: tuple[PackageFile, ...], hostile: PackageFile) -> Digest:
    """The closure digest of ``clean`` plus ``hostile``, as a source serving both would declare it.

    The hostile record is built under a safe path and re-pathed afterwards, because constructing
    it directly is exactly what the path guard refuses.
    """

    record = AssetRecord(
        path="references/placeholder.md",
        kind=classify_asset(hostile.path),
        digest=Digest.of_bytes(hostile.content),
        size=len(hostile.content),
        executable=False,
    )
    object.__setattr__(record, "path", hostile.path)
    return closure_digest([*build_closure(clean).files, record], ())


def _case_path_traversal(rng: random.Random) -> None:
    """A closure whose digest vouches for an unsafe path is never resolved or installed.

    The source declares the digest of what it actually serves, so only path validation stands
    between the hostile closure and the install. Each attempt must be refused with nothing
    written, and the install destination guard must refuse the same path on its own.
    """

    name = f"skill-{_token(rng)}"
    clean = _package(name, _token(rng).encode())
    hostile_paths = (
        f"references/../../../{_token(rng)}.md",
        f"references//{_token(rng)}.md",
        f"references/./{_token(rng)}.md",
        f"references\\{_token(rng)}.md",
    )
    for hostile_path in hostile_paths:
        hostile = PackageFile(hostile_path, _token(rng).encode())
        descriptor = replace(
            _descriptor(name, clean), digest=_closure_digest_including(clean, hostile)
        )
        source = _source(descriptor, (*clean, hostile))
        with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
            work = Path(scratch)
            _refused(
                CapabilityPathError,
                partial(_resolve_and_install, source, name, work),
                written=work,
                what=f"a closure carrying {hostile_path!r}",
            )
            _expect(
                CapabilityPathError,
                partial(
                    install_module._guard_destination,
                    work / "installed",
                    f"{name}/{hostile_path}",
                ),
            )


def _case_unreviewed_asset_type(rng: random.Random) -> None:
    """A package that ships an unreviewed asset type is never resolved or installed.

    The hostile file is really on disk, so the closure the source declares is the closure it
    serves; only asset classification stands between it and the install.
    """

    name = f"skill-{_token(rng)}"
    clean = _package(name, _token(rng).encode())
    executable_instruction = tuple(
        PackageFile(item.path, item.content, executable=True) if item.path == "SKILL.md" else item
        for item in clean
    )
    hostile_closures = [
        (*clean, PackageFile("scripts/payload.exe", b"MZ")),
        (*clean, PackageFile("bin/helper", b"#!/bin/sh\n", executable=True)),
        (*clean, PackageFile("references/archive.zip", b"PK")),
        (*clean, PackageFile("data/model.pkl", b"pickle")),
        (*clean, PackageFile("__pycache__/module.cpython-312.pyc", b"bytecode")),
        executable_instruction,
    ]
    for files in hostile_closures:
        offending = next(item.path for item in files if item not in clean)
        with (
            tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as sources,
            tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch,
        ):
            _write_directory(Path(sources), name, files)
            work = Path(scratch)
            _refused(
                CapabilityAssetTypeError,
                partial(
                    _resolve_and_install,
                    _directory_source(Path(sources), SourceKind.USER),
                    name,
                    work,
                ),
                written=work,
                what=f"an unreviewed asset {offending!r}",
            )


def _case_unknown_license_redistribution_hold(rng: random.Random) -> None:
    """An absent or unreviewed license holds redistribution even with an approval."""

    for license_id in (None, "GPL-3.0-only", "Proprietary"):
        name = f"skill-{_token(rng)}"
        files = _package(name, _token(rng).encode(), license_id=license_id)
        resolved = CapabilityResolver([_served(SourceKind.USER, files)]).resolve_with_files(
            [_request(name)]
        )
        with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
            base = Path(scratch)
            _expect(
                CapabilityLicenseHoldError,
                partial(
                    install_lock,
                    resolved,
                    install_root=base / "installed",
                    capabilities_root=base / "store",
                    approvals=_approve(resolved.lock, [name]),
                ),
            )
            _assert(not (base / "store").exists(), "a held package was materialized")
    parent, dependency = f"skill-{_token(rng)}", f"dep-{_token(rng)}"
    dependency_files = _package(dependency, _token(rng).encode(), license_id=None)
    pin = DependencyPin(dependency, "1", build_closure(dependency_files).digest)
    parent_files = _package(parent, _token(rng).encode(), dependencies=(pin,))
    resolved = CapabilityResolver(
        [_served(SourceKind.USER, parent_files, dependency_files)]
    ).resolve_with_files([_request(parent)])
    with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
        base = Path(scratch)
        _expect(
            CapabilityLicenseHoldError,
            partial(
                install_lock,
                resolved,
                install_root=base / "installed",
                capabilities_root=base / "store",
                approvals=_approve(resolved.lock, [parent, dependency]),
            ),
        )


def _case_unauthenticated_asset_requires_approval(rng: random.Random) -> None:
    """Every non-project source is unauthenticated and needs the exact-digest approval."""

    for kind in (SourceKind.USER, SourceKind.GIT, SourceKind.ARMORY):
        name = f"skill-{_token(rng)}"
        files = _package(name, _token(rng).encode(), extra={"scripts/helper.py": b"print(1)\n"})
        other = _package(name, _token(rng).encode(), extra={"scripts/helper.py": b"print(2)\n"})
        resolved = CapabilityResolver([_served(kind, files)]).resolve_with_files([_request(name)])
        elsewhere = CapabilityResolver([_served(kind, other)]).resolve([_request(name)])
        entry = resolved.lock.entries[0]
        _assert(
            entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED,
            f"a {kind.value} asset must not classify as authenticated without a signature",
        )
        with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
            base = Path(scratch)
            insufficient = (
                NO_APPROVALS,
                _approve(resolved.lock, ["some-other-package"]),
                _approve(elsewhere, [name]),
            )
            for approvals in insufficient:
                _expect(
                    CapabilityApprovalRequiredError,
                    partial(
                        install_lock,
                        resolved,
                        install_root=base / "installed",
                        capabilities_root=base / "store",
                        approvals=approvals,
                    ),
                )
            _assert(
                not (base / "installed").exists() and not (base / "store").exists(),
                "an unapproved asset wrote files",
            )


def _case_approval_superseded_after_drift(rng: random.Random) -> None:
    """An approval recorded for one closure never applies to a drifted closure."""

    name = f"skill-{_token(rng)}"
    payload = _token(rng).encode()
    reviewed = _package(name, payload, extra={"scripts/run.py": b"print('reviewed')\n"})
    drifted = _replace(reviewed, "scripts/run.py", b"print('changed after approval')\n")
    first = CapabilityResolver([_served(SourceKind.USER, reviewed)]).resolve_with_files(
        [_request(name)]
    )
    second = CapabilityResolver([_served(SourceKind.USER, drifted)]).resolve_with_files(
        [_request(name)]
    )
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("capability-gate-policy", approval_registry=registry)
    schema = approval_module.materialize_approval_schema(
        first.lock.entries[0], first.lock, requesting_principal_id=_RUN.id
    )
    registry.record_approval(schema, [_OPERATOR])

    approved = approval_module.approved_capabilities(
        first.lock, evaluator, requesting_principal_id=_RUN.id
    )
    superseded = approval_module.approved_capabilities(
        second.lock, evaluator, requesting_principal_id=_RUN.id
    )

    _assert(
        approved.covers(first.lock.entries[0], first.lock.digest()),
        "a recorded approval must allow its exact closure",
    )
    _assert(
        not superseded.covers(second.lock.entries[0], second.lock.digest()),
        "an approval survived a sibling script change",
    )
    with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
        base = Path(scratch)
        for approvals in (superseded, approved):
            _expect(
                CapabilityApprovalRequiredError,
                partial(
                    install_lock,
                    second,
                    install_root=base / "installed",
                    capabilities_root=base / "store",
                    approvals=approvals,
                ),
            )
        _assert(not (base / "store").exists(), "a superseded approval materialized the closure")


def _written_files(root: Path) -> list[str]:
    """Files and links under ``root``; an empty directory does not count as reaching it."""

    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_symlink() or path.is_file()
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` with its bytes, so a comparison sees a change, not just a loss."""

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _content(path: Path) -> bytes | None:
    """The file's bytes, or ``None`` when it no longer exists."""

    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _case_uninstall_retains_evidence_and_ledger(rng: random.Random) -> None:
    """Uninstall removes only owned, unmodified files and revokes authority, nothing else.

    The layout mirrors the CLI: the install root sits beside the content store and the lockfile
    under one state directory, and the ledger and raw evidence live elsewhere in the workspace.
    Every retained file is compared byte for byte with what it held before the uninstall.
    """

    name = f"skill-{_token(rng)}"
    files = _package(name, _token(rng).encode(), extra={"scripts/helper.py": b"print(1)\n"})
    with tempfile.TemporaryDirectory(prefix="enginery-capability-gate-") as scratch:
        base = Path(scratch)
        _write_directory(base / "packages", name, files)
        resolved = CapabilityResolver(
            [_directory_source(base / "packages", SourceKind.USER)]
        ).resolve_with_files([_request(name)])
        state = base / ".enginery"
        install_root, store = state / "installed", state / "capabilities"
        lockfile = state / "capabilities.lock.json"
        install_lock(
            resolved,
            install_root=install_root,
            capabilities_root=store,
            approvals=_approve(resolved.lock, [name]),
        )
        write_lock(resolved.lock, lockfile)
        ledger = base / "ledger.db"
        ledger.write_bytes(b"ledger bytes")
        raw_evidence = base / "evidence" / "raw.bin"
        raw_evidence.parent.mkdir()
        raw_evidence.write_bytes(b"raw evidence bytes")
        foreign = install_root / name / "operator-notes.txt"
        foreign.write_bytes(b"operator notes")
        edited = install_root / name / "scripts" / "helper.py"
        edited.chmod(0o644)
        edited.write_bytes(b"print('edited locally')\n")
        lockfile_before, store_before = lockfile.read_bytes(), _snapshot(store)

        report = uninstall_install(install_root)

        _assert(_content(ledger) == b"ledger bytes", "uninstall touched the ledger")
        _assert(_content(raw_evidence) == b"raw evidence bytes", "uninstall touched evidence")
        _assert(_content(lockfile) == lockfile_before, "uninstall changed the lockfile")
        _assert(_snapshot(store) == store_before, "uninstall changed the content store")
        _assert(_content(foreign) == b"operator notes", "uninstall deleted a foreign file")
        _assert(
            _content(edited) == b"print('edited locally')\n",
            "uninstall deleted a file modified after install",
        )
        removed_unmodified = f"{name}/SKILL.md" in report.removed
        _assert(
            removed_unmodified and not (install_root / name / "SKILL.md").exists(),
            "uninstall did not remove an owned, unmodified file",
        )
        digest = resolved.lock.digest().hex_value
        revocation = install_root / f"{REVOKED_RECORD_PREFIX}{digest}-1.json"
        _assert(
            load_owned_manifest(install_root) is None
            and report.revocation_record == revocation
            and revocation.is_file(),
            "uninstall did not revoke the install record",
        )
        _expect(CapabilityNotInstalledError, lambda: uninstall_install(install_root))


def _scratch_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="enginery-capability-gate-"))


CASES: dict[str, Callable[[random.Random], None]] = {
    "unsigned_external_asset": _case_unsigned_external_asset,
    "wrong_signer": _case_wrong_signer,
    "digest_swap": _case_digest_swap,
    "license_mismatch": _case_license_mismatch,
    "run_introduced_without_approval": _case_run_introduced_capability_blocks_without_approval,
    "active_run_mutation": _case_active_run_mutation,
    "sibling_script_drift": _case_sibling_script_drift,
    "reference_drift": _case_reference_drift,
    "dependency_pin_drift": _case_dependency_pin_drift,
    "symlink_escape": _case_symlink_escape,
    "path_traversal": _case_path_traversal,
    "unreviewed_asset_type": _case_unreviewed_asset_type,
    "unknown_license_redistribution_hold": _case_unknown_license_redistribution_hold,
    "unauthenticated_asset_requires_approval": _case_unauthenticated_asset_requires_approval,
    "approval_superseded_after_drift": _case_approval_superseded_after_drift,
    "uninstall_retains_evidence_and_ledger": _case_uninstall_retains_evidence_and_ledger,
}


def run_gate() -> None:
    seed = secrets.randbits(64)
    rng = random.Random(seed)
    for case in CASES.values():
        case(rng)
    print(f"PASS capability adversarial cases={len(CASES)} seed={seed}")


if __name__ == "__main__":
    run_gate()
