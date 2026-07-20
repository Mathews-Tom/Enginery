from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _provenance(status: ProvenanceStatus, *, signer_key_id: str | None = None) -> ProvenanceRecord:
    return ProvenanceRecord(
        status=status, source_label="test-source", signer_key_id=signer_key_id, verified_at=_NOW
    )


def _entry(
    name: str = "skill-a",
    *,
    introduced_by_run: bool = False,
    status: ProvenanceStatus = ProvenanceStatus.LOCAL_TRUSTED,
    signer_key_id: str | None = None,
) -> LockedCapability:
    return LockedCapability(
        name=name,
        version="1",
        digest=Digest.of_bytes(name.encode()),
        provenance=_provenance(status, signer_key_id=signer_key_id),
        license="MIT",
        introduced_by_run=introduced_by_run,
    )


def test_reviewed_base_entry_never_requires_approval() -> None:
    entry = _entry(introduced_by_run=False, status=ProvenanceStatus.LOCAL_TRUSTED)

    assert entry.requires_human_approval() is False


def test_run_introduced_unauthenticated_entry_requires_approval() -> None:
    entry = _entry(introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED)

    assert entry.requires_human_approval() is True


def test_run_introduced_authenticated_entry_still_requires_approval() -> None:
    """A signature is trust evidence, not a bypass of the run-introduced hard rule."""

    entry = _entry(
        introduced_by_run=True, status=ProvenanceStatus.AUTHENTICATED, signer_key_id="key-1"
    )

    assert entry.requires_human_approval() is True


def test_reviewed_base_entry_cannot_carry_non_local_provenance() -> None:
    with pytest.raises(InvalidInputError, match="local_trusted"):
        _entry(introduced_by_run=False, status=ProvenanceStatus.UNAUTHENTICATED)


def test_run_introduced_entry_cannot_carry_local_trusted_provenance() -> None:
    with pytest.raises(InvalidInputError, match="local_trusted"):
        _entry(introduced_by_run=True, status=ProvenanceStatus.LOCAL_TRUSTED)


def test_authenticated_provenance_requires_signer_key_id() -> None:
    with pytest.raises(InvalidInputError, match="signer_key_id"):
        ProvenanceRecord(
            status=ProvenanceStatus.AUTHENTICATED,
            source_label="test-source",
            signer_key_id=None,
            verified_at=_NOW,
        )


def test_lock_rejects_duplicate_names() -> None:
    with pytest.raises(InvalidInputError, match="unique"):
        CapabilityLock(entries=(_entry("skill-a"), _entry("skill-a")))


def test_lock_orders_entries_deterministically() -> None:
    lock = CapabilityLock(entries=(_entry("skill-b"), _entry("skill-a")))

    assert [entry.name for entry in lock.entries] == ["skill-a", "skill-b"]


def test_lock_digest_is_stable_for_equivalent_entry_sets() -> None:
    lock_one = CapabilityLock(entries=(_entry("skill-a"), _entry("skill-b")))
    lock_two = CapabilityLock(entries=(_entry("skill-b"), _entry("skill-a")))

    assert lock_one.digest() == lock_two.digest()


def test_lock_digest_changes_when_an_entry_digest_changes() -> None:
    lock_one = CapabilityLock(entries=(_entry("skill-a"),))
    changed = LockedCapability(
        name="skill-a",
        version="1",
        digest=Digest.of_bytes(b"different-bytes"),
        provenance=_provenance(ProvenanceStatus.LOCAL_TRUSTED),
        license="MIT",
        introduced_by_run=False,
    )
    lock_two = CapabilityLock(entries=(changed,))

    assert lock_one.digest() != lock_two.digest()


def test_pending_approval_reports_only_entries_needing_a_decision() -> None:
    approved_already = _entry("skill-a", introduced_by_run=False)
    pending = _entry("skill-b", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED)
    lock = CapabilityLock(entries=(approved_already, pending))

    assert lock.pending_approval() == (pending,)


def test_lock_get_returns_none_for_missing_name() -> None:
    lock = CapabilityLock(entries=(_entry("skill-a"),))

    assert lock.get("missing") is None
    assert lock.get("skill-a") is not None
