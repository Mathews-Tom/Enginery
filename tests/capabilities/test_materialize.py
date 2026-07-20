from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.capabilities.errors import CapabilityApprovalRequiredError, CapabilityIntegrityError
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.materialize import materialize_capability, materialize_lock
from enginery.domain.digests import Digest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _entry(
    name: str,
    payload: bytes,
    *,
    introduced_by_run: bool = False,
    status: ProvenanceStatus = ProvenanceStatus.LOCAL_TRUSTED,
) -> LockedCapability:
    return LockedCapability(
        name=name,
        version="1",
        digest=Digest.of_bytes(payload),
        provenance=ProvenanceRecord(
            status=status,
            source_label="test-source",
            signer_key_id="key-1" if status is ProvenanceStatus.AUTHENTICATED else None,
            verified_at=_NOW,
        ),
        license="MIT",
        introduced_by_run=introduced_by_run,
    )


def test_materialize_capability_writes_content_addressed_bytes(tmp_path: Path) -> None:
    entry = _entry("skill-a", b"payload-a")

    path = materialize_capability(entry, b"payload-a", root=tmp_path)

    assert path.read_bytes() == b"payload-a"
    assert path.name == entry.digest.hex_value


def test_materialize_capability_rejects_mismatched_bytes(tmp_path: Path) -> None:
    entry = _entry("skill-a", b"payload-a")

    with pytest.raises(CapabilityIntegrityError):
        materialize_capability(entry, b"tampered-bytes", root=tmp_path)


def test_materialize_capability_is_idempotent_for_identical_bytes(tmp_path: Path) -> None:
    entry = _entry("skill-a", b"payload-a")

    first = materialize_capability(entry, b"payload-a", root=tmp_path)
    second = materialize_capability(entry, b"payload-a", root=tmp_path)

    assert first == second
    assert first.read_bytes() == b"payload-a"


def test_materialize_lock_writes_every_approved_entry(tmp_path: Path) -> None:
    trusted = _entry("skill-a", b"payload-a", introduced_by_run=False)
    lock = CapabilityLock(entries=(trusted,))

    materialized = materialize_lock(lock, {trusted.digest: b"payload-a"}, root=tmp_path)

    assert materialized["skill-a"].read_bytes() == b"payload-a"


def test_materialize_lock_refuses_unapproved_run_introduced_entry(tmp_path: Path) -> None:
    pending = _entry(
        "skill-a", b"payload-a", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    lock = CapabilityLock(entries=(pending,))

    with pytest.raises(CapabilityApprovalRequiredError):
        materialize_lock(lock, {pending.digest: b"payload-a"}, root=tmp_path)


def test_materialize_lock_allows_run_introduced_entry_once_approved(tmp_path: Path) -> None:
    pending = _entry(
        "skill-a", b"payload-a", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    lock = CapabilityLock(entries=(pending,))

    materialized = materialize_lock(
        lock, {pending.digest: b"payload-a"}, root=tmp_path, approved_names=frozenset({"skill-a"})
    )

    assert materialized["skill-a"].read_bytes() == b"payload-a"


def test_materialize_lock_rejects_missing_content_for_a_locked_digest(tmp_path: Path) -> None:
    trusted = _entry("skill-a", b"payload-a", introduced_by_run=False)
    lock = CapabilityLock(entries=(trusted,))

    with pytest.raises(CapabilityIntegrityError):
        materialize_lock(lock, {}, root=tmp_path)
