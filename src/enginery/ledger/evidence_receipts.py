"""Canonical receipt artifacts and bounded raw captures in the existing artifact store."""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    CaptureArtifact,
    EvidenceReceipt,
    MediaDecodeResult,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import ArtifactDigestMismatchError, ArtifactMissingError

MediaValidator = Callable[[bytes, str], MediaDecodeResult]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


@contextlib.contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Open every directory component without following links."""
    if not path.is_absolute() or ".." in path.parts:
        raise InvalidInputError("evidence artifact store requires an absolute, link-safe path")
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


class EvidenceReceiptRepository:
    """Persist receipts by canonical digest, never in a parallel store or ledger.

    Composition supplies the existing privately initialized ArtifactStore and the
    trusted media decoder. The validator is injected to preserve the ledger's
    inward-only dependency boundary. Loading rechecks capture integrity and decode
    metadata; neither persistence nor a successful decode constitutes acceptance.
    """

    def __init__(self, store: ArtifactStore, *, media_validator: MediaValidator) -> None:
        self._store = store
        self._media_validator = media_validator
        for path in (store.root, store.root / "objects", store.root / "tmp"):
            with _directory(path) as descriptor:
                os.fchmod(descriptor, 0o700)

    def _prepare(self, digest: Digest) -> None:
        path = self._store.path_for(digest)
        relative = path.relative_to(self._store.root)
        with _directory(self._store.root) as root:
            descriptor = os.dup(root)
            try:
                for part in relative.parts[:-1]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = next_descriptor
                    os.fchmod(descriptor, 0o700)
                try:
                    metadata = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if not stat.S_ISREG(metadata.st_mode):
                    raise InvalidInputError("evidence artifact path must be a regular, non-link file")
            finally:
                os.close(descriptor)

    def _read(self, digest: Digest) -> bytes:
        path = self._store.path_for(digest)
        try:
            with _directory(path.parent) as parent:
                descriptor = os.open(
                    path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK,
                    dir_fd=parent,
                )
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise InvalidInputError("evidence artifact must be a regular, non-link file")
                    if metadata.st_size > MAX_EVIDENCE_BYTES:
                        raise InvalidInputError("evidence artifact exceeds the 1 MiB limit")
                    with os.fdopen(os.dup(descriptor), "rb") as handle:
                        data = handle.read(MAX_EVIDENCE_BYTES + 1)
                finally:
                    os.close(descriptor)
        except FileNotFoundError as error:
            raise ArtifactMissingError(
                "evidence references missing artifact bytes", details={"digest": str(digest)},
            ) from error
        if len(data) > MAX_EVIDENCE_BYTES:
            raise InvalidInputError("evidence artifact exceeds the 1 MiB limit")
        if Digest.of_bytes(data) != digest:
            raise ArtifactDigestMismatchError(
                "evidence artifact bytes do not match their digest", details={"digest": str(digest)},
            )
        return data

    def _publish(self, data: bytes, *, media_type: str) -> Digest:
        if not isinstance(data, bytes) or len(data) > MAX_EVIDENCE_BYTES:
            raise InvalidInputError("evidence artifact requires bytes within the 1 MiB limit")
        expected = Digest.of_bytes(data)
        self._prepare(expected)
        actual = self._store.publish_bytes(data, media_type=media_type)
        self._read(actual)
        return actual

    def publish_capture(self, data: bytes, *, media_type: str) -> CaptureArtifact:
        """Retain bounded raw bytes and the decoder's actual result, including failures."""
        if not isinstance(data, bytes) or len(data) > MAX_EVIDENCE_BYTES:
            raise InvalidInputError("raw capture requires bytes within the 1 MiB limit")
        capture = CaptureArtifact(
            Digest.of_bytes(data), media_type, len(data), self._media_validator(data, media_type),
        )
        self._publish(data, media_type=media_type)
        return capture

    def _verify_captures(self, receipt: EvidenceReceipt) -> None:
        for capture in receipt.capture_manifest.artifacts:
            data = self._read(capture.digest)
            if len(data) != capture.size_bytes:
                raise InvalidInputError("capture size does not match its retained raw bytes")
            if self._media_validator(data, capture.media_type) != capture.decode:
                raise InvalidInputError("capture decode result does not match its retained raw bytes")

    def publish(self, receipt: EvidenceReceipt) -> Digest:
        """Publish only after every raw reference is retained and digest-verified."""
        self._verify_captures(receipt)
        data = json.dumps(
            receipt.to_state(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        digest = self._publish(data, media_type="application/json")
        if digest != receipt.digest:
            raise ArtifactDigestMismatchError("receipt artifact does not match its canonical digest")
        return digest

    def load(self, digest: Digest) -> EvidenceReceipt:
        """Load canonical schema-known receipt bytes and revalidate every raw capture."""
        raw = self._read(digest)
        try:
            state: object = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise InvalidInputError("evidence receipt artifact is not valid bounded JSON") from error
        receipt = EvidenceReceipt.from_state(state)
        if receipt.digest != digest:
            raise ArtifactDigestMismatchError("evidence receipt artifact is not canonical")
        self._verify_captures(receipt)
        return receipt


__all__ = ["EvidenceReceiptRepository", "MediaValidator"]
