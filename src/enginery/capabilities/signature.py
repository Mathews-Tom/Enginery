"""Pinned-key signature verification for externally sourced capabilities.

TLS authenticates a connection, not a publisher: a capability fetched over
HTTPS can still have been swapped by anyone who controls the source's
content, including the source operator itself. This module verifies an
Ed25519 detached signature over a capability's content digest against a
small, explicitly pinned set of trusted publisher keys -- never against a
key claim the source itself supplies unverified.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from enginery.application.delivery_ports import CapabilityDescriptor
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


class SignatureVerificationError(InvalidInputError):
    """Raised when a supplied signature does not verify against a pinned key."""


@dataclass(frozen=True, slots=True)
class PinnedPublisherKey:
    """One operator-pinned Ed25519 publisher public key."""

    key_id: str
    public_key: Ed25519PublicKey

    def __post_init__(self) -> None:
        if not self.key_id.strip():
            raise InvalidInputError("pinned publisher key_id must be non-blank")

    @classmethod
    def from_raw_bytes(cls, key_id: str, raw_public_key: bytes) -> PinnedPublisherKey:
        try:
            public_key = Ed25519PublicKey.from_public_bytes(raw_public_key)
        except ValueError as error:
            raise InvalidInputError(
                "pinned publisher key must be 32 raw Ed25519 public-key bytes"
            ) from error
        return cls(key_id=key_id, public_key=public_key)


class PinnedKeyring:
    """An operator-configured, closed set of trusted publisher keys.

    There is no discovery mechanism: a key enters this keyring only through
    explicit construction, matching "authenticated provenance means a
    verified signature chain against pinned identity."
    """

    def __init__(self, keys: Iterable[PinnedPublisherKey] = ()) -> None:
        self._keys: dict[str, PinnedPublisherKey] = {}
        for key in keys:
            if key.key_id in self._keys:
                raise InvalidInputError(
                    "duplicate pinned publisher key_id", details={"key_id": key.key_id}
                )
            self._keys[key.key_id] = key

    def verify(self, *, digest: Digest, signature: bytes, key_id: str) -> str:
        """Verify ``signature`` over ``digest`` against the pinned ``key_id``.

        Returns ``key_id`` on success. Raises :class:`SignatureVerificationError`
        for an unpinned key, a malformed signature, or a signature that does
        not verify.
        """

        pinned = self._keys.get(key_id)
        if pinned is None:
            raise SignatureVerificationError(
                "signature claims an unpinned publisher key", details={"key_id": key_id}
            )
        try:
            pinned.public_key.verify(signature, digest.hex_value.encode("ascii"))
        except InvalidSignature as error:
            raise SignatureVerificationError(
                "signature did not verify against the pinned publisher key",
                details={"key_id": key_id},
            ) from error
        return key_id

    def try_verify(
        self, *, digest: Digest, signature: bytes | None, key_id: str | None
    ) -> str | None:
        """A non-raising helper suited to ``CapabilityResolver``'s signature verifier slot."""

        if signature is None or key_id is None:
            return None
        try:
            return self.verify(digest=digest, signature=signature, key_id=key_id)
        except SignatureVerificationError:
            return None


def descriptor_verifier(keyring: PinnedKeyring) -> Callable[[CapabilityDescriptor], str | None]:
    """Adapt a :class:`PinnedKeyring` to the resolver's signature-verifier slot."""

    def verify(descriptor: CapabilityDescriptor) -> str | None:
        return keyring.try_verify(
            digest=descriptor.digest,
            signature=descriptor.signature,
            key_id=descriptor.signer_key_id,
        )

    return verify


__all__ = [
    "PinnedKeyring",
    "PinnedPublisherKey",
    "SignatureVerificationError",
    "descriptor_verifier",
]
