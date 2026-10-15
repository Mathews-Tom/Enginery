from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest

from enginery.capabilities import install as install_module
from enginery.capabilities.compatibility import HostProfile
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityIncompatibleError,
    CapabilityInstallConflictError,
    CapabilityLicenseHoldError,
    CapabilityNotInstalledError,
)
from enginery.capabilities.install import (
    OWNED_MANIFEST_NAME,
    REVOKED_RECORD_NAME,
    BlockerKind,
    build_install_plan,
    install_lock,
    load_owned_manifest,
    uninstall_install,
)
from enginery.capabilities.manifest import PackageCompatibility
from enginery.capabilities.materialize import digest_path
from enginery.capabilities.resolver import (
    CapabilityRequest,
    CapabilityResolver,
    ResolvedCapabilities,
)
from enginery.capabilities.sources import CapabilitySource
from tests.support.capabilities import (
    HOST,
    package_files,
    project_source,
    user_source,
)


def _resolve(
    *sources: CapabilitySource,
    names: tuple[str, ...] = ("skill-a",),
    host: HostProfile = HOST,
    report_shadowed: bool = False,
) -> ResolvedCapabilities:
    resolver = CapabilityResolver(list(sources), host=host)
    return resolver.resolve_with_files(
        [CapabilityRequest(name=name, version="1") for name in names],
        report_shadowed=report_shadowed,
    )


def _tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")} if root.exists() else set()


def _install(
    resolved: ResolvedCapabilities, tmp_path: Path, approved: frozenset[str] = frozenset()
) -> Path:
    install_lock(
        resolved,
        install_root=tmp_path / "installed",
        capabilities_root=tmp_path / "store",
        approved_names=approved,
    )
    return tmp_path / "installed"


# --------------------------------------------------------------------------- preview


def test_preview_has_no_side_effects_and_lists_the_exact_footprint(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = tmp_path / "installed"

    plan = build_install_plan(resolved, install_root=root)

    assert not root.exists()
    assert not (tmp_path / "store").exists()
    assert plan.ready
    assert plan.install_root == root
    assert {item.destination for item in plan.files} == {
        "skill-a/SKILL.md",
        "skill-a/capability.json",
        "skill-a/references/guide.md",
    }
    entry = resolved.lock.entries[0]
    by_destination = {item.destination: item for item in plan.files}
    for record in entry.files:
        planned = by_destination[f"skill-a/{record.path}"]
        assert (planned.size, planned.digest, planned.kind) == (
            record.size,
            record.digest,
            record.kind,
        )


def test_preview_reports_required_approvals_with_exact_digests(tmp_path: Path) -> None:
    resolved = _resolve(user_source(package_files("skill-a")))

    plan = build_install_plan(resolved, install_root=tmp_path / "installed")

    assert not plan.ready
    (approval,) = plan.approvals_required
    assert approval.name == "skill-a"
    assert approval.closure_digest == resolved.lock.entries[0].digest
    assert approval.lock_digest == resolved.lock.digest()
    assert [blocker.kind for blocker in plan.blockers] == [BlockerKind.APPROVAL_REQUIRED]
    document = plan.to_json()
    assert document["ready"] is False
    assert document["lock_digest"] == str(resolved.lock.digest())


def test_preview_is_ready_once_the_approval_is_supplied(tmp_path: Path) -> None:
    resolved = _resolve(user_source(package_files("skill-a")))

    plan = build_install_plan(
        resolved, install_root=tmp_path / "installed", approved_names=frozenset({"skill-a"})
    )

    assert plan.ready
    assert plan.approvals_required == ()


def test_preview_reports_the_selected_source_and_every_shadowed_alternative(
    tmp_path: Path,
) -> None:
    project = project_source(package_files("skill-a"))
    other = package_files("skill-a", body=b"# a different body\n")
    resolved = _resolve(user_source(other), project, report_shadowed=True)

    plan = build_install_plan(resolved, install_root=tmp_path / "installed")

    (package,) = plan.packages
    assert package.selection.selected.kind.value == "project"
    assert [candidate.kind.value for candidate in package.selection.shadowed] == ["user"]
    source = package.to_json()["source"]
    assert isinstance(source, dict)
    (shadowed,) = source["shadowed"]
    assert shadowed["same_closure"] is False
    assert package.trust.value == "local_trusted"


def test_preview_reports_license_and_compatibility_holds(tmp_path: Path) -> None:
    files = package_files(
        "skill-a",
        license_id=None,
        compatibility=PackageCompatibility(platforms=("darwin",)),
    )
    resolved = _resolve(user_source(files), host=HostProfile(1, "linux"))

    plan = build_install_plan(
        resolved, install_root=tmp_path / "installed", approved_names=frozenset({"skill-a"})
    )

    kinds = {blocker.kind for blocker in plan.blockers}
    assert kinds == {BlockerKind.LICENSE_HOLD, BlockerKind.INCOMPATIBLE}
    (package,) = plan.packages
    assert package.redistribution_blocked
    assert not package.receipt.compatible


def test_a_project_package_without_a_license_is_not_a_redistribution(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a", license_id=None)))

    plan = build_install_plan(resolved, install_root=tmp_path / "installed")

    assert plan.ready


# --------------------------------------------------------------------------- install


def test_install_writes_only_inside_the_install_root_and_records_ownership(
    tmp_path: Path,
) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))

    root = _install(resolved, tmp_path)

    manifest = load_owned_manifest(root)
    assert manifest is not None
    assert manifest.lock_digest == resolved.lock.digest()
    (package,) = manifest.packages
    entry = resolved.lock.entries[0]
    assert package.closure_digest == entry.digest
    assert {item.path for item in package.files} == {f"skill-a/{item.path}" for item in entry.files}
    for owned in package.files:
        target = root / owned.path
        assert target.read_bytes() and stat.S_IMODE(target.stat().st_mode) == 0o444
    assert _tree(tmp_path) >= {"installed", "store", f"installed/{OWNED_MANIFEST_NAME}"}
    assert digest_path(tmp_path / "store", entry.digest).is_dir()


def test_install_twice_is_idempotent(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)
    before = _tree(root)

    report = install_lock(resolved, install_root=root, capabilities_root=tmp_path / "store")

    assert report.already_installed
    assert report.files_written == ()
    assert _tree(root) == before


def test_install_refuses_an_unapproved_run_introduced_package_and_writes_nothing(
    tmp_path: Path,
) -> None:
    resolved = _resolve(user_source(package_files("skill-a")))

    with pytest.raises(CapabilityApprovalRequiredError):
        _install(resolved, tmp_path)

    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / "store").exists()


def test_install_with_an_approval_records_the_approved_closure(tmp_path: Path) -> None:
    resolved = _resolve(user_source(package_files("skill-a")))

    root = _install(resolved, tmp_path, frozenset({"skill-a"}))

    manifest = load_owned_manifest(root)
    assert manifest is not None
    assert manifest.approved_closures == (("skill-a", resolved.lock.entries[0].digest),)


def test_a_license_hold_blocks_install_even_with_an_approval(tmp_path: Path) -> None:
    resolved = _resolve(user_source(package_files("skill-a", license_id="GPL-3.0-only")))

    with pytest.raises(CapabilityLicenseHoldError):
        _install(resolved, tmp_path, frozenset({"skill-a"}))

    assert not (tmp_path / "installed").exists()


def test_an_incompatible_package_is_refused(tmp_path: Path) -> None:
    files = package_files("skill-a", compatibility=PackageCompatibility(platforms=("darwin",)))
    resolved = _resolve(project_source(files), host=HostProfile(1, "linux"))

    with pytest.raises(CapabilityIncompatibleError):
        _install(resolved, tmp_path)

    assert not (tmp_path / "installed").exists()


def test_install_refuses_to_overwrite_a_file_it_does_not_own(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = tmp_path / "installed"
    (root / "skill-a").mkdir(parents=True)
    foreign = root / "skill-a" / "SKILL.md"
    foreign.write_bytes(b"not ours\n")

    with pytest.raises(CapabilityInstallConflictError):
        _install(resolved, tmp_path)

    assert foreign.read_bytes() == b"not ours\n"
    assert not (root / OWNED_MANIFEST_NAME).exists()


def test_install_refuses_a_symlinked_destination_directory(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = tmp_path / "installed"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "skill-a").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CapabilityInstallConflictError):
        _install(resolved, tmp_path)

    assert list(outside.iterdir()) == []


def test_a_second_lock_cannot_install_over_an_active_install(tmp_path: Path) -> None:
    first = _resolve(project_source(package_files("skill-a")))
    root = _install(first, tmp_path)
    second = _resolve(project_source(package_files("skill-a", body=b"# changed\n")))

    with pytest.raises(CapabilityInstallConflictError):
        install_lock(second, install_root=root, capabilities_root=tmp_path / "store")


def test_a_failed_install_leaves_no_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))

    def explode(path: Path, document: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(install_module, "_write_json_atomically", explode)

    with pytest.raises(OSError, match="disk full"):
        _install(resolved, tmp_path)

    root = tmp_path / "installed"
    assert not any(path.is_file() for path in root.rglob("*"))
    assert not (root / "skill-a").exists()


# --------------------------------------------------------------------------- uninstall


def test_uninstall_removes_only_owned_files_and_reports_what_it_retained(
    tmp_path: Path,
) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)
    foreign = root / "skill-a" / "notes.txt"
    foreign.write_bytes(b"operator notes\n")

    report = uninstall_install(root)

    assert set(report.removed) == {
        "skill-a/SKILL.md",
        "skill-a/capability.json",
        "skill-a/references/guide.md",
    }
    assert foreign.read_bytes() == b"operator notes\n"
    assert not (root / "skill-a" / "SKILL.md").exists()
    assert not (root / "skill-a" / "references").exists()
    assert not (root / OWNED_MANIFEST_NAME).exists()
    assert report.lock_digest == resolved.lock.digest()


def test_uninstall_retains_a_file_modified_after_install(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)
    edited = root / "skill-a" / "SKILL.md"
    edited.chmod(0o644)
    edited.write_bytes(b"locally edited\n")

    report = uninstall_install(root)

    assert edited.read_bytes() == b"locally edited\n"
    assert [(item.path, item.reason) for item in report.retained] == [
        ("skill-a/SKILL.md", "modified after install")
    ]
    assert "skill-a/SKILL.md" not in report.removed


def test_uninstall_never_touches_the_store_lockfile_ledger_or_evidence(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)
    lockfile = tmp_path / "capabilities.lock.json"
    lockfile.write_text("{}\n", encoding="utf-8")
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"ledger bytes")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "raw.bin").write_bytes(b"raw evidence")
    store_before = _tree(tmp_path / "store")

    uninstall_install(root)

    assert _tree(tmp_path / "store") == store_before
    assert lockfile.read_text(encoding="utf-8") == "{}\n"
    assert ledger.read_bytes() == b"ledger bytes"
    assert (evidence / "raw.bin").read_bytes() == b"raw evidence"


def test_uninstall_revokes_the_install_record_with_a_tombstone(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)

    report = uninstall_install(root)

    assert load_owned_manifest(root) is None
    assert report.revocation_record == root / REVOKED_RECORD_NAME
    tombstone = json.loads((root / REVOKED_RECORD_NAME).read_text(encoding="utf-8"))
    assert tombstone["revoked_lock_digest"] == str(resolved.lock.digest())
    assert sorted(tombstone["removed"]) == sorted(report.removed)
    with pytest.raises(CapabilityNotInstalledError):
        uninstall_install(root)


def test_uninstall_does_not_follow_a_swapped_in_symlink(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "SKILL.md"
    victim.write_bytes(b"outside the install root\n")
    package_directory = root / "skill-a"
    shutil.rmtree(package_directory)
    package_directory.symlink_to(outside, target_is_directory=True)

    report = uninstall_install(root)

    assert victim.read_bytes() == b"outside the install root\n"
    assert report.removed == ()
    assert report.retained
    assert all(item.reason for item in report.retained)


def test_a_reinstall_after_uninstall_succeeds(tmp_path: Path) -> None:
    resolved = _resolve(project_source(package_files("skill-a")))
    root = _install(resolved, tmp_path)
    uninstall_install(root)

    report = install_lock(resolved, install_root=root, capabilities_root=tmp_path / "store")

    assert not report.already_installed
    assert load_owned_manifest(root) is not None
    assert (root / "skill-a" / "SKILL.md").is_file()
