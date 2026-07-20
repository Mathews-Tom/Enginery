from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.serialization import lock_from_json, lock_to_json, read_lock, write_lock
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _lock() -> CapabilityLock:
    trusted = LockedCapability(
        name="skill-a",
        version="1",
        digest=Digest.of_bytes(b"payload-a"),
        provenance=ProvenanceRecord(
            status=ProvenanceStatus.LOCAL_TRUSTED,
            source_label="local",
            signer_key_id=None,
            verified_at=_NOW,
        ),
        license="MIT",
        introduced_by_run=False,
    )
    authenticated = LockedCapability(
        name="skill-b",
        version="2",
        digest=Digest.of_bytes(b"payload-b"),
        provenance=ProvenanceRecord(
            status=ProvenanceStatus.AUTHENTICATED,
            source_label="armory",
            signer_key_id="publisher-1",
            verified_at=_NOW,
        ),
        license=None,
        introduced_by_run=True,
    )
    return CapabilityLock(entries=(trusted, authenticated))


def test_lock_round_trips_through_json() -> None:
    lock = _lock()

    restored = lock_from_json(lock_to_json(lock))

    assert restored.digest() == lock.digest()
    assert restored.get("skill-b") is not None
    assert restored.get("skill-b").provenance.signer_key_id == "publisher-1"  # type: ignore[union-attr]


def test_write_and_read_lock_round_trip(tmp_path: Path) -> None:
    lock = _lock()
    path = tmp_path / "nested" / "capabilities.lock.json"

    write_lock(lock, path)
    restored = read_lock(path)

    assert restored.digest() == lock.digest()


def test_lock_from_json_rejects_missing_entries_list() -> None:
    with pytest.raises(InvalidInputError, match="entries"):
        lock_from_json({})


def test_lock_from_json_rejects_malformed_digest() -> None:
    payload = lock_to_json(_lock())
    payload["entries"][0]["digest"] = "not-a-digest"  # type: ignore[index]

    with pytest.raises(InvalidInputError):
        lock_from_json(payload)


def test_read_lock_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(InvalidInputError, match="not valid JSON"):
        read_lock(path)


def test_read_lock_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="unable to read"):
        read_lock(tmp_path / "missing.json")
