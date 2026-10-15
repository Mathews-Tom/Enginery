"""The capability gate passes as shipped and every closure case fails once its guard is removed.

Each guard-removal row below disables exactly one guard in the library (the drift check, the
materialized-closure re-verification, a dependency pin check, link handling, path or asset
validation, the executable-bit rule, the license hold, the approval requirement, approval
binding, the digest check inside an approval, or the uninstall scope). The two retention rows
remove no library guard: they inject a destructive uninstall to prove the retention assertions
can fail. A case that still passed with its guard gone would be vacuous, and so would one that
fails for some other reason: every row names the message the case itself raises, and the
removal must produce exactly that.
"""

from __future__ import annotations

import random
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import adversarial_capability_gate as gate
from enginery.adapters import directory_capability
from enginery.application.delivery_ports import PackageFile
from enginery.capabilities import approval, closure, install, materialize
from enginery.capabilities.lock import ApprovedCapabilities, CapabilityLock, LockedCapability
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.domain.digests import Digest
from enginery.domain.policy_decision import PolicyAction
from enginery.policy.schemas import ApprovalSchema

_ZERO_DIGEST = "sha256:" + "0" * 64

Mutation = Callable[[pytest.MonkeyPatch], None]


class _EqualToAnything:
    """Stands in for a digest that always matches, as a removed digest comparison would."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False


def _drop_the_drift_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        CapabilityResolver,
        "_enforce_no_drift",
        lambda self, request, descriptor, *, previous_lock: None,
    )


def _drop_the_dependency_pin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every place a dependency pin is enforced.

    The resolver compares each resolved dependency with its pin, and the lock separately
    refuses an entry whose pin no locked dependency satisfies. With only the resolver
    comparison gone the lock still refuses a tampered dependency, so both go together.
    """

    def resolve_without_comparing(
        self: CapabilityResolver, package: closure.PackageClosure, **context: Any
    ) -> None:
        for pin in package.manifest.dependencies:
            self._resolve_one(CapabilityRequest(name=pin.name, version=pin.version), **context)

    monkeypatch.setattr(CapabilityResolver, "_resolve_dependencies", resolve_without_comparing)
    monkeypatch.setattr(LockedCapability, "pin", lambda self: _EqualToAnything())


def _skip_materialized_reverification(monkeypatch: pytest.MonkeyPatch) -> None:
    def lenient(entry: LockedCapability, root: Path) -> tuple[Path, None]:
        return materialize.digest_path(root, entry.digest), None

    monkeypatch.setattr(materialize, "load_verified_closure", lenient)


def _follow_links_when_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    def follow(directory: Path) -> tuple[PackageFile, ...]:
        return tuple(
            PackageFile(path.relative_to(directory).as_posix(), path.read_bytes())
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        )

    monkeypatch.setattr(directory_capability, "read_package_directory", follow)


def _write_through_links_when_installing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "_require_real_directory", lambda path, *, what: None)


def _accept_every_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(closure, "_path_problem", lambda raw: None)


def _accept_every_asset_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        closure,
        "classify_asset",
        lambda path, *, executable=False, content=None: closure.AssetKind.DATA,
    )


def _ignore_the_executable_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the executable-bit rule in asset classification and in the asset record check."""

    classify = closure.classify_asset

    def classify_without_the_bit(
        path: str, *, executable: bool = False, content: bytes | None = None
    ) -> closure.AssetKind:
        del executable
        return classify(path, content=content)

    def check_without_the_bit(self: closure.AssetRecord) -> None:
        closure.normalize_path(self.path)

    monkeypatch.setattr(closure, "classify_asset", classify_without_the_bit)
    monkeypatch.setattr(closure.AssetRecord, "__post_init__", check_without_the_bit)


def _never_hold_a_license(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LockedCapability, "redistribution_blocked", lambda self: False)


def _never_require_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LockedCapability, "requires_human_approval", lambda self: False)


def _bind_approval_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def constant(
        entry: LockedCapability, lock: CapabilityLock, *, requesting_principal_id: str
    ) -> ApprovalSchema:
        return ApprovalSchema(
            action=PolicyAction.CAPABILITY_MATERIALIZE,
            requested_capability=f"{entry.name}@{entry.version}",
            capability_locks=_ZERO_DIGEST,
            diff_or_artifact_digest=_ZERO_DIGEST,
            capability_introduced_by_run=entry.introduced_by_run,
            requesting_principal_id=requesting_principal_id,
        )

    monkeypatch.setattr(approval, "materialize_approval_schema", constant)


def _approve_by_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the digest comparison, so an approval for a name covers any closure of it."""

    def by_name(self: ApprovedCapabilities, entry: LockedCapability, lock_digest: Digest) -> bool:
        return not entry.requires_human_approval() or entry.name in self.names()

    monkeypatch.setattr(ApprovedCapabilities, "covers", by_name)


def _remove_directories_recursively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        install, "_remove_if_empty", lambda directory: shutil.rmtree(directory, ignore_errors=True)
    )


def _ignore_modification_after_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make uninstall, and only uninstall, treat every file still present as unmodified.

    Install compares file digests as well, so replacing the helper everywhere would stop the
    package from being installed at all instead of removing the uninstall guard.
    """

    uninstall = install.uninstall_install

    def trusting_uninstall(install_root: Path) -> install.UninstallReport:
        with monkeypatch.context() as scope:
            scope.setattr(install, "_file_digest", lambda path: _EqualToAnything())
            return uninstall(install_root)

    monkeypatch.setattr(gate, "uninstall_install", trusting_uninstall)


def _sweep_after_uninstall(monkeypatch: pytest.MonkeyPatch, ancestor: int) -> None:
    """Make uninstall also clear an ancestor of the install root, as a careless revocation does."""

    uninstall = install.uninstall_install

    def sweeping(install_root: Path) -> install.UninstallReport:
        report = uninstall(install_root)
        shutil.rmtree(install_root.parents[ancestor], ignore_errors=True)
        return report

    monkeypatch.setattr(gate, "uninstall_install", sweeping)


def _clear_the_state_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The content store and the lockfile live beside the install root."""

    _sweep_after_uninstall(monkeypatch, 0)


def _clear_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger and raw evidence live elsewhere in the workspace."""

    _sweep_after_uninstall(monkeypatch, 1)


GUARD_REMOVALS: tuple[tuple[str, str, Mutation, str], ...] = (
    (
        "sibling_script_drift",
        "drift-check",
        _drop_the_drift_check,
        r"CapabilityLockDriftError was not raised",
    ),
    (
        "reference_drift",
        "reverification",
        _skip_materialized_reverification,
        r"CapabilityIntegrityError was not raised",
    ),
    (
        "dependency_pin_drift",
        "pin-check",
        _drop_the_dependency_pin_check,
        r"CapabilityDependencyError was not raised",
    ),
    (
        "symlink_escape",
        "directory-links",
        _follow_links_when_reading,
        r"CapabilitySymlinkError was not raised",
    ),
    (
        "symlink_escape",
        "install-links",
        _write_through_links_when_installing,
        r"CapabilityInstallConflictError was not raised",
    ),
    ("path_traversal", "path-validation", _accept_every_path, r"reached the filesystem"),
    (
        "unreviewed_asset_type",
        "asset-classification",
        _accept_every_asset_type,
        r"reached the filesystem",
    ),
    (
        "unreviewed_asset_type",
        "executable-bit",
        _ignore_the_executable_bit,
        r"an unreviewed asset 'SKILL\.md' reached the filesystem",
    ),
    (
        "unknown_license_redistribution_hold",
        "license-hold",
        _never_hold_a_license,
        r"CapabilityLicenseHoldError was not raised",
    ),
    (
        "unauthenticated_asset_requires_approval",
        "approval",
        _never_require_approval,
        r"CapabilityApprovalRequiredError was not raised",
    ),
    (
        "approval_superseded_after_drift",
        "approval-binding",
        _bind_approval_to_nothing,
        r"an approval survived a sibling script change",
    ),
    (
        "approval_superseded_after_drift",
        "approval-digest",
        _approve_by_name_only,
        r"CapabilityApprovalRequiredError was not raised",
    ),
    (
        "uninstall_retains_evidence_and_ledger",
        "owned-scope",
        _remove_directories_recursively,
        r"uninstall deleted a foreign file",
    ),
    (
        "uninstall_retains_evidence_and_ledger",
        "digest-check",
        _ignore_modification_after_install,
        r"modified after install",
    ),
    (
        "uninstall_retains_evidence_and_ledger",
        "state-retention",
        _clear_the_state_directory,
        r"uninstall changed the lockfile",
    ),
    (
        "uninstall_retains_evidence_and_ledger",
        "workspace-retention",
        _clear_the_workspace,
        r"uninstall touched the ledger",
    ),
)


def test_every_adversarial_capability_case_passes_as_shipped() -> None:
    gate.run_gate()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_closure_cases_pass_for_several_generated_packages(seed: int) -> None:
    rng = random.Random(seed)
    for name in list(gate.CASES)[6:]:
        gate.CASES[name](rng)


def test_every_closure_case_has_a_guard_removal_below() -> None:
    covered = {row[0] for row in GUARD_REMOVALS}

    assert covered == set(list(gate.CASES)[6:])


@pytest.mark.parametrize(
    ("case", "label", "mutate", "refusal"),
    GUARD_REMOVALS,
    ids=[f"{row[0]}-{row[1]}" for row in GUARD_REMOVALS],
)
def test_a_case_fails_with_its_own_message_once_its_guard_is_removed(
    case: str, label: str, mutate: Mutation, refusal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate.CASES[case](random.Random(7))
    mutate(monkeypatch)

    with pytest.raises(RuntimeError, match=refusal):
        gate.CASES[case](random.Random(7))
