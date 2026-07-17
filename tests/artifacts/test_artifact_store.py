from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.digests import Digest
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import (
    ArtifactDigestMismatchError,
    ArtifactMissingError,
    RawCredentialDetectedError,
)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def test_publish_and_read_round_trips(store: ArtifactStore) -> None:
    digest = store.publish_bytes(b"hello world", media_type="text/plain")
    assert digest == Digest.of_bytes(b"hello world")
    assert store.read_bytes(digest) == b"hello world"


def test_publish_is_content_addressed_and_idempotent(store: ArtifactStore) -> None:
    first = store.publish_bytes(b"same content")
    second = store.publish_bytes(b"same content")
    assert first == second
    assert store.path_for(first) == store.path_for(second)


def test_publish_writes_a_two_phase_atomic_file(store: ArtifactStore) -> None:
    digest = store.publish_bytes(b"payload bytes")
    path = store.path_for(digest)
    assert path.is_file()
    assert path.read_bytes() == b"payload bytes"
    # no leftover temp files after a clean publish
    assert list((store.root / "tmp").iterdir()) == []


def test_read_missing_digest_raises(store: ArtifactStore) -> None:
    fabricated = Digest.of_bytes(b"never published")
    with pytest.raises(ArtifactMissingError):
        store.read_bytes(fabricated)


def test_read_corrupted_bytes_raises_digest_mismatch(store: ArtifactStore) -> None:
    digest = store.publish_bytes(b"original content")
    store.path_for(digest).write_bytes(b"tampered content")
    with pytest.raises(ArtifactDigestMismatchError):
        store.read_bytes(digest)


def test_verify_reports_missing_and_corrupted_and_healthy(store: ArtifactStore) -> None:
    fabricated = Digest.of_bytes(b"never published")
    assert store.verify(fabricated) is False

    digest = store.publish_bytes(b"healthy content")
    assert store.verify(digest) is True

    store.path_for(digest).write_bytes(b"corrupted now")
    assert store.verify(digest) is False


def test_publish_rejects_credential_shaped_text(store: ArtifactStore) -> None:
    with pytest.raises(RawCredentialDetectedError):
        store.publish_bytes(b"AKIAABCDEFGHIJKLMNOP", media_type="text/plain")


def test_publish_does_not_scan_binary_media_types(store: ArtifactStore) -> None:
    # AWS-key-shaped bytes are fine when the caller declares a non-text
    # media type; the heuristic only applies to text-ish content.
    digest = store.publish_bytes(b"AKIAABCDEFGHIJKLMNOP", media_type="application/octet-stream")
    assert store.read_bytes(digest) == b"AKIAABCDEFGHIJKLMNOP"


def test_rejected_publish_leaves_no_temp_file(store: ArtifactStore) -> None:
    with pytest.raises(RawCredentialDetectedError):
        store.publish_bytes(b"AKIAABCDEFGHIJKLMNOP", media_type="text/plain")
    assert list((store.root / "tmp").iterdir()) == []


def test_iter_digests_lists_every_published_object(store: ArtifactStore) -> None:
    first = store.publish_bytes(b"one")
    second = store.publish_bytes(b"two")
    assert set(store.iter_digests()) == {first, second}


def test_sweep_abandoned_temp_files_removes_only_old_files(store: ArtifactStore) -> None:
    stale = store.root / "tmp" / "artifact-stale"
    stale.write_bytes(b"orphaned")
    removed = store.sweep_abandoned_temp_files(older_than_seconds=-1)
    assert stale in removed
    assert not stale.exists()


def test_sweep_leaves_fresh_temp_files_alone(store: ArtifactStore) -> None:
    fresh = store.root / "tmp" / "artifact-fresh"
    fresh.write_bytes(b"in flight")
    removed = store.sweep_abandoned_temp_files(older_than_seconds=3600)
    assert fresh not in removed
    assert fresh.exists()
