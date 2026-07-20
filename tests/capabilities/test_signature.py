from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from enginery.application.delivery_ports import CapabilityDescriptor
from enginery.capabilities.signature import (
    PinnedKeyring,
    PinnedPublisherKey,
    SignatureVerificationError,
    descriptor_verifier,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


def _keypair(key_id: str) -> tuple[Ed25519PrivateKey, PinnedPublisherKey]:
    private_key = Ed25519PrivateKey.generate()
    raw_public = private_key.public_key().public_bytes_raw()
    return private_key, PinnedPublisherKey.from_raw_bytes(key_id, raw_public)


def _sign(private_key: Ed25519PrivateKey, digest: Digest) -> bytes:
    return private_key.sign(digest.hex_value.encode("ascii"))


def test_signature_verifies_against_its_pinned_key() -> None:
    private_key, pinned = _keypair("publisher-1")
    keyring = PinnedKeyring([pinned])
    digest = Digest.of_bytes(b"payload")

    verified = keyring.verify(
        digest=digest, signature=_sign(private_key, digest), key_id="publisher-1"
    )

    assert verified == "publisher-1"


def test_signature_from_an_unpinned_key_is_rejected() -> None:
    private_key, _pinned = _keypair("publisher-1")
    keyring = PinnedKeyring([])
    digest = Digest.of_bytes(b"payload")

    with pytest.raises(SignatureVerificationError, match="unpinned"):
        keyring.verify(digest=digest, signature=_sign(private_key, digest), key_id="publisher-1")


def test_wrong_signer_key_is_rejected() -> None:
    """A signature produced by one key must not verify under a different pinned key_id."""

    signer_private_key, _signer_pinned = _keypair("publisher-1")
    _other_private_key, other_pinned = _keypair("publisher-2")
    keyring = PinnedKeyring([other_pinned])
    digest = Digest.of_bytes(b"payload")
    signature = _sign(signer_private_key, digest)

    with pytest.raises(SignatureVerificationError):
        keyring.verify(digest=digest, signature=signature, key_id="publisher-2")


def test_signature_over_a_different_digest_is_rejected() -> None:
    private_key, pinned = _keypair("publisher-1")
    keyring = PinnedKeyring([pinned])
    signed_digest = Digest.of_bytes(b"declared-payload")
    swapped_digest = Digest.of_bytes(b"swapped-payload")

    with pytest.raises(SignatureVerificationError):
        keyring.verify(
            digest=swapped_digest, signature=_sign(private_key, signed_digest), key_id="publisher-1"
        )


def test_duplicate_pinned_key_id_is_rejected() -> None:
    _private_key, pinned = _keypair("publisher-1")
    with pytest.raises(InvalidInputError, match="duplicate"):
        PinnedKeyring([pinned, pinned])


def test_try_verify_returns_none_instead_of_raising_for_a_missing_signature() -> None:
    keyring = PinnedKeyring([])
    digest = Digest.of_bytes(b"payload")

    assert keyring.try_verify(digest=digest, signature=None, key_id=None) is None


def test_try_verify_returns_none_for_an_invalid_signature() -> None:
    private_key, _pinned = _keypair("publisher-1")
    keyring = PinnedKeyring([])
    digest = Digest.of_bytes(b"payload")

    result = keyring.try_verify(
        digest=digest, signature=_sign(private_key, digest), key_id="publisher-1"
    )

    assert result is None


def test_descriptor_verifier_upgrades_a_correctly_signed_descriptor() -> None:
    private_key, pinned = _keypair("publisher-1")
    keyring = PinnedKeyring([pinned])
    digest = Digest.of_bytes(b"payload")
    descriptor = CapabilityDescriptor(
        name="skill-a",
        version="1",
        digest=digest,
        provenance="external-registry",
        signature=_sign(private_key, digest),
        signer_key_id="publisher-1",
    )

    verify = descriptor_verifier(keyring)

    assert verify(descriptor) == "publisher-1"


def test_descriptor_verifier_returns_none_for_an_unsigned_descriptor() -> None:
    keyring = PinnedKeyring([])
    descriptor = CapabilityDescriptor(
        name="skill-a", version="1", digest=Digest.of_bytes(b"payload"), provenance="tls-only"
    )

    verify = descriptor_verifier(keyring)

    assert verify(descriptor) is None


def test_descriptor_requires_signature_and_signer_key_id_together() -> None:
    with pytest.raises(ValueError, match="must both be present"):
        CapabilityDescriptor(
            name="skill-a",
            version="1",
            digest=Digest.of_bytes(b"payload"),
            provenance="external-registry",
            signature=b"orphan-signature",
        )
