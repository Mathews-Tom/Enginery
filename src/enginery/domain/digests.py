"""Content-addressed digests.

Every content-addressed asset in the design — workflow-definition content,
work-item bound-field snapshots, artifact bytes, and node-attempt inputs —
is identified by a SHA-256 digest computed the same way everywhere. This
module is the single place that canonicalizes a payload before hashing so
two equal domain values always produce the same digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError

_ALGORITHM = "sha256"
_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Digest:
    """A verified SHA-256 content digest."""

    algorithm: str
    hex_value: str

    def __post_init__(self) -> None:
        if self.algorithm != _ALGORITHM:
            raise InvalidInputError(
                f"unsupported digest algorithm {self.algorithm!r}; only {_ALGORITHM!r} is accepted",
                details={"algorithm": self.algorithm},
            )
        if not _HEX_PATTERN.fullmatch(self.hex_value):
            raise InvalidInputError(
                "digest hex value must be exactly 64 lowercase hexadecimal characters",
                details={"hex_value": self.hex_value},
            )

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.hex_value}"

    @classmethod
    def of_bytes(cls, data: bytes) -> Digest:
        return cls(algorithm=_ALGORITHM, hex_value=hashlib.sha256(data).hexdigest())

    @classmethod
    def of_json(cls, payload: object) -> Digest:
        """Hash a JSON-serializable payload using a canonical encoding.

        Sorted keys and compact separators guarantee that two structurally
        equal payloads always hash to the same digest regardless of
        construction order, matching the "content digest" and "bound-field
        digest" requirements throughout the design.
        """
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return cls.of_bytes(canonical.encode("utf-8"))


__all__ = ["Digest"]
