from __future__ import annotations

import pytest

from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import CapabilityDescriptor
from enginery.capabilities.errors import (
    CapabilityDigestMismatchError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
)
from enginery.capabilities.lock import CapabilityLock, ProvenanceStatus
from enginery.capabilities.resolver import (
    CapabilityNotFoundError,
    CapabilityRequest,
    CapabilityResolver,
)
from enginery.domain.digests import Digest


class _FakeSource:
    """A minimal in-memory ``CapabilitySourcePort`` for resolver tests."""

    def __init__(
        self, descriptors: dict[tuple[str, str], CapabilityDescriptor], content: dict[str, bytes]
    ) -> None:
        self._descriptors = descriptors
        self._content = content

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityDescriptor, ...]:  # pragma: no cover - unused
        return tuple(self._descriptors.values())

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        return self._descriptors.get((name, version))

    def fetch(self, name: str, version: str) -> bytes:
        return self._content[f"{name}:{version}"]


def _descriptor(
    name: str, version: str, payload: bytes, *, provenance: str = "repository-local"
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name, version=version, digest=Digest.of_bytes(payload), provenance=provenance
    )


def test_reviewed_base_capability_resolves_as_local_trusted() -> None:
    source = _FakeSource(
        {("skill-a", "1"): _descriptor("skill-a", "1", b"payload-a")},
        {"skill-a:1": b"payload-a"},
    )
    resolver = CapabilityResolver([source], reviewed_base=frozenset({("skill-a", "1")}))

    lock = resolver.resolve([CapabilityRequest(name="skill-a", version="1")])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.introduced_by_run is False
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.requires_human_approval() is False


def test_capability_outside_reviewed_base_is_introduced_by_run_and_unauthenticated() -> None:
    source = _FakeSource(
        {("skill-a", "1"): _descriptor("skill-a", "1", b"payload-a")},
        {"skill-a:1": b"payload-a"},
    )
    resolver = CapabilityResolver([source])

    lock = resolver.resolve([CapabilityRequest(name="skill-a", version="1")])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.introduced_by_run is True
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.requires_human_approval() is True


def test_signature_verifier_upgrades_run_introduced_capability_to_authenticated() -> None:
    source = _FakeSource(
        {("skill-a", "1"): _descriptor("skill-a", "1", b"payload-a")},
        {"skill-a:1": b"payload-a"},
    )
    resolver = CapabilityResolver([source], signature_verifier=lambda descriptor: "signer-1")

    lock = resolver.resolve([CapabilityRequest(name="skill-a", version="1")])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.provenance.status is ProvenanceStatus.AUTHENTICATED
    assert entry.provenance.signer_key_id == "signer-1"
    # Authenticated provenance still does not exempt a run-introduced capability.
    assert entry.requires_human_approval() is True


def test_unknown_capability_raises_not_found() -> None:
    source = _FakeSource({}, {})
    resolver = CapabilityResolver([source])

    with pytest.raises(CapabilityNotFoundError):
        resolver.resolve([CapabilityRequest(name="missing", version="1")])


def test_license_mismatch_is_rejected_before_lock_construction() -> None:
    """A caller-declared expected license that disagrees with what the source
    reports must fail closed rather than silently lock under a different
    license."""

    descriptor = CapabilityDescriptor(
        name="skill-a",
        version="1",
        digest=Digest.of_bytes(b"payload-a"),
        provenance="repository-local",
        license="GPL-3.0",
    )
    source = _FakeSource({("skill-a", "1"): descriptor}, {"skill-a:1": b"payload-a"})
    resolver = CapabilityResolver([source])

    with pytest.raises(CapabilityLicenseMismatchError):
        resolver.resolve([CapabilityRequest(name="skill-a", version="1", expected_license="MIT")])


def test_digest_swap_is_rejected_before_lock_construction() -> None:
    """A source that reports one digest but serves different bytes must fail closed."""

    descriptor = _descriptor("skill-a", "1", b"declared-payload")
    source = _FakeSource({("skill-a", "1"): descriptor}, {"skill-a:1": b"swapped-payload"})
    resolver = CapabilityResolver([source])

    with pytest.raises(CapabilityDigestMismatchError):
        resolver.resolve([CapabilityRequest(name="skill-a", version="1")])


def test_mutable_reference_drift_against_an_in_flight_lock_is_rejected() -> None:
    """A source that later resolves the same locked version to a different digest
    must never silently mutate an in-flight lock."""

    original_source = _FakeSource(
        {("skill-a", "1"): _descriptor("skill-a", "1", b"payload-v1")},
        {"skill-a:1": b"payload-v1"},
    )
    resolver = CapabilityResolver([original_source], reviewed_base=frozenset({("skill-a", "1")}))
    requests = [CapabilityRequest(name="skill-a", version="1")]
    previous_lock: CapabilityLock = resolver.resolve(requests)

    drifted_source = _FakeSource(
        {("skill-a", "1"): _descriptor("skill-a", "1", b"payload-v1-mutated")},
        {"skill-a:1": b"payload-v1-mutated"},
    )
    drift_resolver = CapabilityResolver(
        [drifted_source], reviewed_base=frozenset({("skill-a", "1")})
    )

    with pytest.raises(CapabilityLockDriftError):
        drift_resolver.resolve(
            [CapabilityRequest(name="skill-a", version="1")], previous_lock=previous_lock
        )


def test_stable_reference_re_resolution_does_not_trip_drift_detection() -> None:
    source = _FakeSource(
        {("skill-a", "1"): _descriptor("skill-a", "1", b"payload-v1")},
        {"skill-a:1": b"payload-v1"},
    )
    resolver = CapabilityResolver([source], reviewed_base=frozenset({("skill-a", "1")}))
    previous_lock = resolver.resolve([CapabilityRequest(name="skill-a", version="1")])

    lock_again = resolver.resolve(
        [CapabilityRequest(name="skill-a", version="1")], previous_lock=previous_lock
    )

    assert lock_again.digest() == previous_lock.digest()


def test_no_sources_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CapabilityResolver([])
