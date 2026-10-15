from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.errors import CapabilityLockDriftError
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
    carry_pins,
    check_pins,
)
from enginery.capabilities.manifest import PackageKind
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from tests.support.capabilities import (
    NOW,
    lock_of,
    locked_entry,
    package_files,
    pin_for,
    replace_file,
)


def test_reviewed_base_entry_never_requires_approval() -> None:
    entry = locked_entry(introduced_by_run=False, status=ProvenanceStatus.LOCAL_TRUSTED)

    assert entry.requires_human_approval() is False


def test_run_introduced_unauthenticated_entry_requires_approval() -> None:
    entry = locked_entry(introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED)

    assert entry.requires_human_approval() is True


def test_run_introduced_authenticated_entry_still_requires_approval() -> None:
    """A signature is trust evidence, not a bypass of the run-introduced hard rule."""

    entry = locked_entry(
        introduced_by_run=True, status=ProvenanceStatus.AUTHENTICATED, signer_key_id="key-1"
    )

    assert entry.requires_human_approval() is True


def test_reviewed_base_entry_cannot_carry_non_local_provenance() -> None:
    with pytest.raises(InvalidInputError, match="local_trusted"):
        locked_entry(introduced_by_run=False, status=ProvenanceStatus.UNAUTHENTICATED)


def test_run_introduced_entry_cannot_carry_local_trusted_provenance() -> None:
    with pytest.raises(InvalidInputError, match="local_trusted"):
        locked_entry(introduced_by_run=True, status=ProvenanceStatus.LOCAL_TRUSTED)


def test_authenticated_provenance_requires_signer_key_id() -> None:
    with pytest.raises(InvalidInputError, match="signer_key_id"):
        ProvenanceRecord(
            status=ProvenanceStatus.AUTHENTICATED,
            source_label="test-source",
            signer_key_id=None,
            verified_at=NOW,
        )


def test_entry_rejects_a_digest_that_does_not_match_its_recorded_files() -> None:
    entry = locked_entry("skill-a")

    with pytest.raises(InvalidInputError, match="closure digest"):
        replace(entry, digest=Digest.of_bytes(b"not the closure"))


def test_entry_rejects_files_edited_without_recomputing_the_digest() -> None:
    entry = locked_entry("skill-a")
    other = locked_entry("skill-a", body=b"# edited\n")

    with pytest.raises(InvalidInputError, match="closure digest"):
        replace(entry, files=other.files)


def test_lock_rejects_duplicate_names() -> None:
    with pytest.raises(InvalidInputError, match="unique"):
        CapabilityLock(entries=(locked_entry("skill-a"), locked_entry("skill-a")))


def test_lock_orders_entries_deterministically() -> None:
    lock = lock_of(locked_entry("skill-b"), locked_entry("skill-a"))

    assert [entry.name for entry in lock.entries] == ["skill-a", "skill-b"]


def test_lock_digest_is_stable_for_equivalent_entry_sets() -> None:
    lock_one = lock_of(locked_entry("skill-a"), locked_entry("skill-b"))
    lock_two = lock_of(locked_entry("skill-b"), locked_entry("skill-a"))

    assert lock_one.digest() == lock_two.digest()


def test_lock_digest_ignores_the_verification_timestamp() -> None:
    entry = locked_entry("skill-a")
    later = replace(
        entry, provenance=replace(entry.provenance, verified_at=NOW + timedelta(days=30))
    )

    assert lock_of(entry).digest() == lock_of(later).digest()


def _digest_of(files: tuple[PackageFile, ...]) -> Digest:
    return lock_of(locked_entry(files=files)).digest()


def test_lock_digest_changes_when_a_sibling_file_changes() -> None:
    base = package_files("skill-a", extra={"scripts/run.py": b"print('one')\n"})
    changed = replace_file(base, "scripts/run.py", b"print('two')\n")

    assert _digest_of(base) != _digest_of(changed)


def test_lock_digest_changes_when_a_reference_changes() -> None:
    base = package_files("skill-a")
    changed = replace_file(base, "references/guide.md", b"edited reference")

    assert _digest_of(base) != _digest_of(changed)


def test_lock_digest_changes_when_the_license_changes() -> None:
    entry = locked_entry("skill-a")

    assert lock_of(entry).digest() != lock_of(replace(entry, license="GPL-3.0")).digest()


def test_lock_digest_changes_when_the_origin_revision_changes() -> None:
    entry = locked_entry("skill-a")

    assert lock_of(entry).digest() != lock_of(replace(entry, revision="rev-2")).digest()


def test_lock_digest_changes_when_the_trust_classification_changes() -> None:
    pending = locked_entry("skill-a", introduced_by_run=True)
    authenticated = replace(
        pending,
        provenance=replace(
            pending.provenance, status=ProvenanceStatus.AUTHENTICATED, signer_key_id="key-1"
        ),
    )

    assert lock_of(pending).digest() != lock_of(authenticated).digest()


def test_lock_digest_changes_when_the_package_kind_changes() -> None:
    entry = locked_entry("skill-a")
    changed = replace(entry, kind=PackageKind.HOOK)

    assert lock_of(entry).digest() != lock_of(changed).digest()


def test_dependency_pin_must_be_satisfied_by_a_lock_entry() -> None:
    dependency_files = package_files("dep-a")
    parent_files = package_files("skill-a", dependencies=[pin_for("dep-a", dependency_files)])
    parent = locked_entry(files=parent_files)

    with pytest.raises(InvalidInputError, match="dependency pin"):
        CapabilityLock(entries=(parent,))


def test_dependency_pin_with_a_different_closure_is_not_satisfied() -> None:
    pinned_files = package_files("dep-a", body=b"# pinned\n")
    drifted = locked_entry("dep-a", body=b"# drifted\n")
    pinned = package_files("skill-a", dependencies=[pin_for("dep-a", pinned_files)])
    parent = locked_entry(files=pinned)

    with pytest.raises(InvalidInputError, match="dependency pin"):
        CapabilityLock(entries=(parent, drifted))


def test_dependency_change_changes_the_lock_digest() -> None:
    first_dependency = package_files("dep-a", body=b"# one\n")
    second_dependency = package_files("dep-a", body=b"# two\n")
    first_parent = package_files("skill-a", dependencies=[pin_for("dep-a", first_dependency)])
    first_lock = lock_of(locked_entry(files=first_parent), locked_entry(files=first_dependency))
    second_lock = lock_of(
        locked_entry(
            files=package_files("skill-a", dependencies=[pin_for("dep-a", second_dependency)])
        ),
        locked_entry(files=second_dependency),
    )

    assert first_lock.digest() != second_lock.digest()


def test_pending_approval_reports_only_entries_needing_a_decision() -> None:
    approved_already = locked_entry("skill-a", introduced_by_run=False)
    pending = locked_entry("skill-b", introduced_by_run=True)
    lock = lock_of(approved_already, pending)

    assert lock.pending_approval() == (pending,)


def test_lock_get_returns_none_for_missing_name() -> None:
    lock = lock_of(locked_entry("skill-a"))

    assert lock.get("missing") is None
    assert lock.get("skill-a") is not None


@pytest.mark.parametrize("license_id", [None, "unknown", "NOASSERTION", "GPL-3.0"])
def test_run_introduced_entry_with_unreviewed_license_blocks_redistribution(
    license_id: str | None,
) -> None:
    entry = locked_entry("skill-a", introduced_by_run=True, license_id=license_id)

    assert entry.redistribution_blocked() is True


def test_run_introduced_entry_with_a_permissive_license_is_not_blocked() -> None:
    entry = locked_entry("skill-a", introduced_by_run=True, license_id="MIT")

    assert entry.redistribution_blocked() is False


def test_reviewed_base_entry_without_a_license_is_not_a_redistribution() -> None:
    entry = locked_entry("skill-a", introduced_by_run=False, license_id=None)

    assert entry.redistribution_blocked() is False


def test_locked_capability_requires_a_non_blank_revision() -> None:
    entry = locked_entry("skill-a")

    with pytest.raises(InvalidInputError, match="revision"):
        LockedCapability(
            name=entry.name,
            version=entry.version,
            kind=entry.kind,
            revision=" ",
            digest=entry.digest,
            provenance=entry.provenance,
            license=entry.license,
            introduced_by_run=entry.introduced_by_run,
            files=entry.files,
            receipt=entry.receipt,
        )


def test_provenance_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(InvalidInputError, match="timezone-aware"):
        ProvenanceRecord(
            status=ProvenanceStatus.UNAUTHENTICATED,
            source_label="test-source",
            signer_key_id=None,
            verified_at=datetime(2026, 1, 1),
        )


def test_retained_pins_never_change_the_lock_digest() -> None:
    entry = locked_entry("skill-b")
    pin = pin_for("skill-a", package_files("skill-a"))

    assert CapabilityLock((entry,), (pin,)).digest() == lock_of(entry).digest()


def test_a_retained_pin_cannot_repeat_an_entry_or_another_pin() -> None:
    entry = locked_entry("skill-a")

    with pytest.raises(InvalidInputError, match="repeat"):
        CapabilityLock((entry,), (entry.pin(),))
    with pytest.raises(InvalidInputError, match="unique"):
        CapabilityLock((), (entry.pin(), entry.pin()))


def test_pin_for_consults_entries_and_retained_pins() -> None:
    entry = locked_entry("skill-b")
    retained = pin_for("skill-a", package_files("skill-a"))
    lock = CapabilityLock((entry,), (retained,))

    assert lock.pin_for("skill-b") == entry.pin()
    assert lock.pin_for("skill-a") == retained
    assert lock.pin_for("other") is None


def test_carry_pins_keeps_the_resolved_entries_exactly_and_retains_the_rest() -> None:
    earlier_a = locked_entry("skill-a")
    earlier_b = locked_entry("skill-b")
    previous = CapabilityLock((earlier_a,), (earlier_b.pin(),))
    resolved = lock_of(locked_entry("skill-c"))

    carried = carry_pins(previous, resolved)

    assert carried.entries == resolved.entries
    assert carried.digest() == resolved.digest()
    assert set(carried.retained_pins) == {earlier_a.pin(), earlier_b.pin()}
    assert carry_pins(None, resolved) == resolved


def test_carry_pins_drops_a_retained_pin_the_resolution_replaces() -> None:
    entry = locked_entry("skill-a")
    previous = CapabilityLock((), (entry.pin(),))

    carried = carry_pins(previous, lock_of(entry))

    assert carried.retained_pins == ()
    assert [item.name for item in carried.entries] == ["skill-a"]


def test_a_retained_pin_blocks_a_changed_closure_and_a_different_version() -> None:
    entry = locked_entry("skill-a")
    previous = CapabilityLock((), (entry.pin(),))
    changed = locked_entry("skill-a", body=b"# changed closure\n")
    other_version = locked_entry("skill-a", version="2")

    with pytest.raises(CapabilityLockDriftError, match="closure digest"):
        check_pins(previous, lock_of(changed))
    with pytest.raises(CapabilityLockDriftError, match="different version"):
        carry_pins(previous, lock_of(other_version))
    check_pins(previous, lock_of(entry))
