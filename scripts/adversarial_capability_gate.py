"""Execute generated supply-chain and mutation attacks against capability locking.

Covers an unsigned external asset, a wrong signer, a digest swap, a license
mismatch, a run-introduced capability without approval, and an active-run
mutation (mutable-reference drift against an in-flight lock). Every case
builds a complete package closure (manifest, instruction, and a sibling
reference) and attacks the resolver, lock, or materializer around it.
"""

from __future__ import annotations

import random
import secrets
import string
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from enginery.adapters.git_capability import GitCapabilitySource
from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import CapabilityDescriptor, CapabilityListing, PackageFile
from enginery.capabilities.approval import approved_capabilities, materialize_approval_schema
from enginery.capabilities.closure import build_closure
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityDigestMismatchError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
)
from enginery.capabilities.lock import ApprovedCapabilities, CapabilityLock, ProvenanceStatus
from enginery.capabilities.manifest import PackageKind, PackageManifest, render_manifest
from enginery.capabilities.materialize import materialize_lock
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.signature import PinnedKeyring, PinnedPublisherKey, descriptor_verifier
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator


def _token(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(20))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _expect(exception_type: type[Exception], operation: object) -> None:
    try:
        operation()  # type: ignore[operator]
    except exception_type:
        return
    raise RuntimeError(f"expected {exception_type.__name__} was not raised")


def _package(
    name: str, payload: bytes, *, license_id: str | None = "MIT"
) -> tuple[PackageFile, ...]:
    manifest = PackageManifest(
        name=name, version="1", kind=PackageKind.INSTRUCTION, license=license_id
    )
    return (
        PackageFile("capability.json", render_manifest(manifest)),
        PackageFile("SKILL.md", payload),
        PackageFile("references/notes.md", payload + b"-reference"),
    )


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
            schema = materialize_approval_schema(entry, lock, requesting_principal_id=_RUN.id)
            registry.record_approval(schema, [_OPERATOR])
    return approved_capabilities(lock, evaluator, requesting_principal_id=_RUN.id)


def _scratch_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="enginery-capability-gate-"))


def run_gate() -> None:
    seed = secrets.randbits(64)
    rng = random.Random(seed)
    cases = (
        _case_unsigned_external_asset,
        _case_wrong_signer,
        _case_digest_swap,
        _case_license_mismatch,
        _case_run_introduced_capability_blocks_without_approval,
        _case_active_run_mutation,
    )
    for case in cases:
        case(rng)
    print(f"PASS capability adversarial cases={len(cases)} seed={seed}")


if __name__ == "__main__":
    run_gate()
