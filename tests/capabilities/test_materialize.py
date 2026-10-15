from __future__ import annotations

import dataclasses
import os
import stat
from pathlib import Path

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityAssetTypeError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
)
from enginery.capabilities.lock import ApprovedCapabilities
from enginery.capabilities.manifest import CapabilityPermission, PackageKind
from enginery.capabilities.materialize import (
    digest_path,
    materialize_capability,
    materialize_lock,
    verify_materialized,
)
from tests.support.capabilities import (
    lock_of,
    locked_entry,
    package_files,
    replace_file,
)


def _scripted() -> tuple[PackageFile, ...]:
    return package_files("skill-a", extra={"scripts/run.py": b"print('ok')\n"})


def test_materialize_capability_writes_the_whole_closure(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)

    directory = materialize_capability(entry, files, root=tmp_path)

    assert directory == digest_path(tmp_path, entry.digest)
    for item in files:
        assert (directory / item.path).read_bytes() == item.content


def test_materialize_capability_preserves_the_executable_bit_only_for_scripts(
    tmp_path: Path,
) -> None:
    files = replace_file(_scripted(), "scripts/run.py", b"print('ok')\n", executable=True)
    entry = locked_entry(files=files)

    directory = materialize_capability(entry, files, root=tmp_path)

    assert os.access(directory / "scripts" / "run.py", os.X_OK)
    assert not os.access(directory / "SKILL.md", os.X_OK)
    assert not stat.S_IMODE((directory / "SKILL.md").stat().st_mode) & 0o222


def test_materialize_capability_rejects_a_changed_sibling_script(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    tampered = replace_file(files, "scripts/run.py", b"print('tampered')\n")

    with pytest.raises(CapabilityIntegrityError) as excinfo:
        materialize_capability(entry, tampered, root=tmp_path)

    assert excinfo.value.details["differences"] == ["changed: scripts/run.py"]
    assert not digest_path(tmp_path, entry.digest).exists()


def test_materialize_capability_rejects_an_added_file(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    extended = (*files, PackageFile("references/extra.md", b"smuggled"))

    with pytest.raises(CapabilityIntegrityError) as excinfo:
        materialize_capability(entry, extended, root=tmp_path)

    assert excinfo.value.details["differences"] == ["unexpected: references/extra.md"]


def test_materialize_capability_rejects_a_removed_file(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    reduced = tuple(item for item in files if item.path != "references/guide.md")

    with pytest.raises(CapabilityIntegrityError) as excinfo:
        materialize_capability(entry, reduced, root=tmp_path)

    assert excinfo.value.details["differences"] == ["missing: references/guide.md"]


def test_materialize_capability_rejects_an_unreviewed_asset_before_writing(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    hostile = (*files, PackageFile("scripts/payload.rb", b"puts 1"))

    with pytest.raises(CapabilityAssetTypeError):
        materialize_capability(entry, hostile, root=tmp_path)

    assert not any(tmp_path.rglob("payload.rb"))


def test_materialize_capability_is_idempotent_for_an_identical_closure(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)

    first = materialize_capability(entry, files, root=tmp_path)
    second = materialize_capability(entry, files, root=tmp_path)

    assert first == second
    assert verify_materialized(entry, tmp_path) == first


def test_existing_materialization_is_never_overwritten_and_tampering_is_detected(
    tmp_path: Path,
) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    directory = materialize_capability(entry, files, root=tmp_path)
    target = directory / "scripts" / "run.py"
    target.chmod(0o644)
    target.write_bytes(b"print('tampered on disk')\n")

    with pytest.raises(CapabilityIntegrityError, match="no longer matches"):
        materialize_capability(entry, files, root=tmp_path)

    assert target.read_bytes() == b"print('tampered on disk')\n"


def test_verify_materialized_detects_an_extra_file(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    directory = materialize_capability(entry, files, root=tmp_path)
    (directory / "references" / "smuggled.md").write_text("extra", encoding="utf-8")

    with pytest.raises(CapabilityIntegrityError) as excinfo:
        verify_materialized(entry, tmp_path)

    assert excinfo.value.details["differences"] == ["unexpected: references/smuggled.md"]


def test_verify_materialized_detects_a_missing_file(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    directory = materialize_capability(entry, files, root=tmp_path)
    (directory / "references" / "guide.md").unlink()

    with pytest.raises(CapabilityIntegrityError) as excinfo:
        verify_materialized(entry, tmp_path)

    assert excinfo.value.details["differences"] == ["missing: references/guide.md"]


def test_verify_materialized_detects_a_file_replaced_by_a_symlink(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    directory = materialize_capability(entry, files, root=tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(files[1].content)
    victim = directory / "SKILL.md"
    victim.unlink()
    victim.symlink_to(outside)

    with pytest.raises(CapabilityIntegrityError, match="no longer a valid package"):
        verify_materialized(entry, tmp_path)


def test_verify_materialized_reports_a_missing_directory(tmp_path: Path) -> None:
    entry = locked_entry(files=_scripted())

    with pytest.raises(CapabilityIntegrityError, match="missing"):
        verify_materialized(entry, tmp_path)


def test_materialize_lock_writes_every_approved_entry(tmp_path: Path) -> None:
    files = _scripted()
    trusted = locked_entry(files=files, introduced_by_run=False)

    materialized = materialize_lock(lock_of(trusted), {"skill-a": files}, root=tmp_path)

    assert (materialized["skill-a"] / "SKILL.md").read_bytes() == files[1].content


def test_materialize_lock_refuses_unapproved_run_introduced_entry(tmp_path: Path) -> None:
    files = _scripted()
    pending = locked_entry(files=files, introduced_by_run=True)

    with pytest.raises(CapabilityApprovalRequiredError):
        materialize_lock(lock_of(pending), {"skill-a": files}, root=tmp_path)

    assert not digest_path(tmp_path, pending.digest).exists()


def test_materialize_lock_allows_run_introduced_entry_once_approved(tmp_path: Path) -> None:
    files = _scripted()
    pending = locked_entry(files=files, introduced_by_run=True)

    lock = lock_of(pending)
    materialized = materialize_lock(
        lock,
        {"skill-a": files},
        root=tmp_path,
        approvals=ApprovedCapabilities.for_entries(lock, ["skill-a"]),
    )

    assert (materialized["skill-a"] / "scripts" / "run.py").read_bytes() == b"print('ok')\n"


def test_materialize_lock_refuses_an_approval_computed_for_a_different_lock(
    tmp_path: Path,
) -> None:
    files = _scripted()
    pending = locked_entry(files=files, introduced_by_run=True)
    approved_lock = lock_of(pending)
    drifted_lock = lock_of(pending, locked_entry("skill-b", introduced_by_run=False))
    approvals = ApprovedCapabilities.for_entries(approved_lock, ["skill-a"])

    with pytest.raises(CapabilityApprovalRequiredError):
        materialize_lock(drifted_lock, {"skill-a": files}, root=tmp_path, approvals=approvals)

    assert not digest_path(tmp_path, pending.digest).exists()


@pytest.mark.parametrize("license_id", [None, "unknown", "GPL-3.0"])
def test_materialize_lock_holds_an_approved_entry_with_an_unreviewed_license(
    tmp_path: Path, license_id: str | None
) -> None:
    files = package_files("skill-a", license_id=license_id)
    pending = locked_entry(files=files, introduced_by_run=True, license_id=license_id)

    lock = lock_of(pending)
    with pytest.raises(CapabilityLicenseHoldError):
        materialize_lock(
            lock,
            {"skill-a": files},
            root=tmp_path,
            approvals=ApprovedCapabilities.for_entries(lock, ["skill-a"]),
        )

    assert not digest_path(tmp_path, pending.digest).exists()


def test_materialize_lock_checks_every_entry_before_writing_any(tmp_path: Path) -> None:
    ready_files = package_files("skill-a")
    held_files = package_files("skill-b", license_id=None)
    ready = locked_entry(files=ready_files, introduced_by_run=False)
    held = locked_entry(files=held_files, introduced_by_run=True, license_id=None)

    lock = lock_of(ready, held)
    with pytest.raises(CapabilityLicenseHoldError):
        materialize_lock(
            lock,
            {"skill-a": ready_files, "skill-b": held_files},
            root=tmp_path,
            approvals=ApprovedCapabilities.for_entries(lock, ["skill-b"]),
        )

    assert not digest_path(tmp_path, ready.digest).exists()


def test_materialize_lock_rejects_missing_files_for_a_locked_entry(tmp_path: Path) -> None:
    trusted = locked_entry("skill-a", introduced_by_run=False)

    with pytest.raises(CapabilityIntegrityError, match="no files were supplied"):
        materialize_lock(lock_of(trusted), {}, root=tmp_path)


def test_materialization_rejects_a_lock_entry_with_a_tampered_kind(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    tampered = dataclasses.replace(entry, kind=PackageKind.COMMAND)

    with pytest.raises(CapabilityIntegrityError):
        materialize_capability(tampered, files, root=tmp_path)


def test_materialization_rejects_a_lock_entry_with_tampered_permissions(tmp_path: Path) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    tampered = dataclasses.replace(entry, permissions=(CapabilityPermission.NETWORK_ACCESS,))

    with pytest.raises(CapabilityIntegrityError):
        materialize_capability(tampered, files, root=tmp_path)


def test_verify_materialized_rejects_a_lock_entry_that_disagrees_with_the_manifest(
    tmp_path: Path,
) -> None:
    files = _scripted()
    entry = locked_entry(files=files)
    materialize_capability(entry, files, root=tmp_path)
    tampered = dataclasses.replace(entry, permissions=(CapabilityPermission.PROCESS_EXECUTE,))

    with pytest.raises(CapabilityIntegrityError):
        verify_materialized(tampered, tmp_path)
