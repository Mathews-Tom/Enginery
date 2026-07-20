"""Execute generated supply-chain and mutation attacks against capability locking.

Covers exactly the six cases M10 requires: an unsigned external asset, a
wrong signer, a digest swap, a license mismatch, a run-introduced
capability without approval, and an active-run mutation (mutable-
reference drift against an in-flight lock).
"""

from __future__ import annotations

import random
import secrets
import string
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import CapabilityDescriptor
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityDigestMismatchError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
)
from enginery.capabilities.lock import CapabilityLock, ProvenanceStatus
from enginery.capabilities.materialize import materialize_lock
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.signature import PinnedKeyring, PinnedPublisherKey, descriptor_verifier
from enginery.domain.digests import Digest


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


class _FakeSource:
    def __init__(self, descriptor: CapabilityDescriptor, content: bytes) -> None:
        self._descriptor = descriptor
        self._content = content

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityDescriptor, ...]:  # pragma: no cover - unused
        return (self._descriptor,)

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        if (name, version) == (self._descriptor.name, self._descriptor.version):
            return self._descriptor
        return None

    def fetch(self, name: str, version: str) -> bytes:
        del name, version
        return self._content


def _case_unsigned_external_asset(rng: random.Random) -> None:
    payload = _token(rng).encode()
    descriptor = CapabilityDescriptor(
        name=f"skill-{_token(rng)}",
        version="1",
        digest=Digest.of_bytes(payload),
        provenance="armory",
    )
    resolver = CapabilityResolver([_FakeSource(descriptor, payload)])

    lock = resolver.resolve([CapabilityRequest(name=descriptor.name, version=descriptor.version)])

    entry = lock.get(descriptor.name)
    _assert(entry is not None, "resolution silently dropped an unsigned capability")
    assert entry is not None
    _assert(
        entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED,
        "an unsigned external asset must not classify as authenticated",
    )
    _expect(
        CapabilityApprovalRequiredError,
        lambda: materialize_lock(lock, {entry.digest: payload}, root=_scratch_root()),
    )


def _case_wrong_signer(rng: random.Random) -> None:
    payload = _token(rng).encode()
    digest = Digest.of_bytes(payload)
    actual_signer = Ed25519PrivateKey.generate()
    claimed_signer_key = Ed25519PrivateKey.generate()
    signature = actual_signer.sign(digest.hex_value.encode("ascii"))
    descriptor = CapabilityDescriptor(
        name=f"skill-{_token(rng)}",
        version="1",
        digest=digest,
        provenance="external-registry",
        signature=signature,
        signer_key_id="claimed-signer",
    )
    keyring = PinnedKeyring([PinnedPublisherKey("claimed-signer", claimed_signer_key.public_key())])
    resolver = CapabilityResolver(
        [_FakeSource(descriptor, payload)], signature_verifier=descriptor_verifier(keyring)
    )

    lock = resolver.resolve([CapabilityRequest(name=descriptor.name, version=descriptor.version)])

    entry = lock.get(descriptor.name)
    assert entry is not None
    _assert(
        entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED,
        "a signature from the wrong signer must not verify as authenticated",
    )


def _case_digest_swap(rng: random.Random) -> None:
    declared_payload = _token(rng).encode()
    swapped_payload = _token(rng).encode()
    descriptor = CapabilityDescriptor(
        name=f"skill-{_token(rng)}",
        version="1",
        digest=Digest.of_bytes(declared_payload),
        provenance="armory",
    )
    resolver = CapabilityResolver([_FakeSource(descriptor, swapped_payload)])

    _expect(
        CapabilityDigestMismatchError,
        lambda: resolver.resolve(
            [CapabilityRequest(name=descriptor.name, version=descriptor.version)]
        ),
    )


def _case_license_mismatch(rng: random.Random) -> None:
    payload = _token(rng).encode()
    descriptor = CapabilityDescriptor(
        name=f"skill-{_token(rng)}",
        version="1",
        digest=Digest.of_bytes(payload),
        provenance="armory",
        license="GPL-3.0",
    )
    resolver = CapabilityResolver([_FakeSource(descriptor, payload)])

    _expect(
        CapabilityLicenseMismatchError,
        lambda: resolver.resolve(
            [
                CapabilityRequest(
                    name=descriptor.name, version=descriptor.version, expected_license="MIT"
                )
            ]
        ),
    )


def _case_run_introduced_capability_blocks_without_approval(rng: random.Random) -> None:
    payload = _token(rng).encode()
    descriptor = CapabilityDescriptor(
        name=f"skill-{_token(rng)}",
        version="1",
        digest=Digest.of_bytes(payload),
        provenance="armory",
    )
    resolver = CapabilityResolver([_FakeSource(descriptor, payload)])

    lock = resolver.resolve([CapabilityRequest(name=descriptor.name, version=descriptor.version)])
    entry = lock.get(descriptor.name)
    assert entry is not None
    _assert(
        entry.introduced_by_run is True,
        "capability outside the reviewed base must be run-introduced",
    )

    _expect(
        CapabilityApprovalRequiredError,
        lambda: materialize_lock(lock, {entry.digest: payload}, root=_scratch_root()),
    )
    materialized = materialize_lock(
        lock, {entry.digest: payload}, root=_scratch_root(), approved_names=frozenset({entry.name})
    )
    _assert(entry.name in materialized, "an approved run-introduced capability must materialize")


def _case_active_run_mutation(rng: random.Random) -> None:
    name = f"skill-{_token(rng)}"
    original_payload = _token(rng).encode()
    mutated_payload = _token(rng).encode()
    original_descriptor = CapabilityDescriptor(
        name=name,
        version="1",
        digest=Digest.of_bytes(original_payload),
        provenance="repository-local",
    )
    resolver = CapabilityResolver(
        [_FakeSource(original_descriptor, original_payload)], reviewed_base=frozenset({(name, "1")})
    )
    previous_lock: CapabilityLock = resolver.resolve([CapabilityRequest(name=name, version="1")])

    mutated_descriptor = CapabilityDescriptor(
        name=name,
        version="1",
        digest=Digest.of_bytes(mutated_payload),
        provenance="repository-local",
    )
    mutated_resolver = CapabilityResolver(
        [_FakeSource(mutated_descriptor, mutated_payload)], reviewed_base=frozenset({(name, "1")})
    )

    _expect(
        CapabilityLockDriftError,
        lambda: mutated_resolver.resolve(
            [CapabilityRequest(name=name, version="1")], previous_lock=previous_lock
        ),
    )


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
