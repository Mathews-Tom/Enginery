"""Content-addressed artifact byte storage.

Publication is a two-phase protocol: bytes are streamed to a unique
temporary path inside the store root (so the final rename is same-
filesystem and therefore atomic), ``fsync``ed, then atomically renamed to
their digest path. Only a digest path that already exists on disk may
ever be referenced by ledger metadata —
:func:`enginery.ledger.artifacts.apply_artifact_metadata` requires the
bytes to already be published before it records a reference, matching
"record only already-published artifact metadata."
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from enginery.domain.digests import Digest
from enginery.ledger.errors import ArtifactDigestMismatchError, ArtifactMissingError
from enginery.ledger.redaction import assert_no_raw_credentials

_TEXT_MEDIA_TYPE_PREFIXES = ("text/", "application/json")


class ArtifactStore:
    """A filesystem-backed, content-addressed store rooted at one directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._objects_dir = root / "objects"
        self._tmp_dir = root / "tmp"
        self._objects_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, digest: Digest) -> Path:
        return self._objects_dir / digest.algorithm / digest.hex_value[:2] / digest.hex_value

    def publish_bytes(self, data: bytes, *, media_type: str = "application/octet-stream") -> Digest:
        """Publish ``data`` and return its digest.

        Text-ish media types are scanned for credential-shaped content
        before anything touches disk; a match raises
        :class:`~enginery.ledger.errors.RawCredentialDetectedError` and
        nothing is written. Publishing the same bytes twice is a no-op —
        content addressing means the second call observes the first
        call's file already in place.
        """
        if media_type.startswith(_TEXT_MEDIA_TYPE_PREFIXES):
            assert_no_raw_credentials(data.decode("utf-8", errors="replace"))

        digest = Digest.of_bytes(data)
        final_path = self.path_for(digest)
        if final_path.is_file():
            return digest

        final_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._tmp_dir, prefix="artifact-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, final_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return digest

    def read_bytes(self, digest: Digest) -> bytes:
        """Read and digest-verify the bytes stored for ``digest``.

        Raises :class:`ArtifactMissingError` if no file exists at the
        digest path, or :class:`ArtifactDigestMismatchError` if the bytes
        on disk no longer hash to the digest they are stored under.
        """
        path = self.path_for(digest)
        if not path.is_file():
            raise ArtifactMissingError(
                f"no artifact bytes found for digest {digest}",
                details={"digest": str(digest)},
            )
        data = path.read_bytes()
        actual = Digest.of_bytes(data)
        if actual != digest:
            raise ArtifactDigestMismatchError(
                f"artifact bytes at {path} hash to {actual}, expected {digest}",
                details={"expected": str(digest), "actual": str(actual)},
            )
        return data

    def verify(self, digest: Digest) -> bool:
        """``True`` iff ``digest``'s bytes exist and are not corrupted."""
        try:
            self.read_bytes(digest)
        except (ArtifactMissingError, ArtifactDigestMismatchError):
            return False
        return True

    def iter_digests(self) -> Iterator[Digest]:
        for path in self._objects_dir.rglob("*"):
            if path.is_file():
                algorithm = path.parent.parent.name
                yield Digest(algorithm=algorithm, hex_value=path.name)

    def sweep_abandoned_temp_files(self, *, older_than_seconds: float = 3600) -> tuple[Path, ...]:
        """Delete temp files older than ``older_than_seconds``.

        A temp file only survives past a successful ``publish_bytes`` call
        if the process was interrupted between the write and the atomic
        rename — this is the startup sweep the design requires for
        "abandoned temporary files."
        """
        cutoff = time.time() - older_than_seconds
        removed: list[Path] = []
        for path in self._tmp_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        return tuple(removed)


__all__ = ["ArtifactStore"]
