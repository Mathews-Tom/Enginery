"""End-to-end lifecycle of the committed reviewed fixture package.

The fixture is a real multi-file package (manifest, instruction, reference, script). It is used
the way a project package is: committed to a Git repository and read from one pinned commit,
never from a working tree. Every test goes through actual resolution, materialization, install,
command execution, and uninstall; nothing here reaches the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from enginery.adapters.directory_capability import DirectoryCapabilitySource
from enginery.adapters.git_capability import GitCapabilitySource
from enginery.application.git_environment import repository_isolated_environment
from enginery.capabilities.approval import approved_capabilities, materialize_approval_schema
from enginery.capabilities.closure import AssetKind, build_closure_from_directory
from enginery.capabilities.command import CommandResult, run_package_command
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityCommandOutputError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
    CapabilityLockDriftError,
    CapabilityNotInstalledError,
)
from enginery.capabilities.install import (
    OWNED_MANIFEST_NAME,
    REVOKED_RECORD_PREFIX,
    build_install_plan,
    install_lock,
    uninstall_install,
)
from enginery.capabilities.lock import (
    NO_APPROVALS,
    ApprovedCapabilities,
    CapabilityLock,
    ProvenanceStatus,
)
from enginery.capabilities.materialize import digest_path, verify_materialized
from enginery.capabilities.resolver import (
    CapabilityRequest,
    CapabilityResolver,
    ResolvedCapabilities,
)
from enginery.capabilities.serialization import read_lock, write_lock
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.cli.main import main
from enginery.domain.errors import InvalidInputError
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "capabilities" / "project"
PACKAGE = "word-counter"
VERSION = "1.0.0"
SUBDIR = "capability-packages"
INTERPRETERS = {"python3": sys.executable}
_OPERATOR = AuthorityPrincipal(
    id="operator-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_RUN = AuthorityPrincipal(
    id="run-1", principal_type=PrincipalType.AGENT, role="run", authorization_source="coordinator"
)
_PACKAGE_FILES = {
    "SKILL.md",
    "capability.json",
    "references/usage.md",
    "scripts/count_words.py",
}


class _Reviewed(NamedTuple):
    """A repository whose ``commit`` holds the fixture package under ``SUBDIR``."""

    repository: Path
    commit: str


def _git_environment() -> dict[str, str]:
    return {
        **repository_isolated_environment(),
        "GIT_AUTHOR_NAME": "Fixture Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=_git_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_all(repository: Path, message: str) -> str:
    """Commit every change under ``repository`` and return the new commit sha."""

    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def reviewed(tmp_path: Path) -> _Reviewed:
    """The fixture package committed to a fresh repository: its commit is the reviewed base."""

    if shutil.which("git") is None:
        pytest.skip("git is required")
    repository = tmp_path / "reviewed"
    repository.mkdir()
    shutil.copytree(FIXTURE_ROOT, repository / SUBDIR)
    _git(repository, "init", "-q", "-b", "main")
    return _Reviewed(repository, _commit_all(repository, "add the reviewed capability package"))


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "user-packages"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def _resolve_at(
    repository: Path, commit: str, *, previous_lock: CapabilityLock | None = None
) -> ResolvedCapabilities:
    """Resolve the package as the project source pinned to ``commit``."""

    port = GitCapabilitySource(repository=str(repository), revision=commit, package_root=SUBDIR)
    source = CapabilitySource(kind=SourceKind.PROJECT, label=f"project:{repository}", port=port)
    return CapabilityResolver([source]).resolve_with_files(
        [CapabilityRequest(name=PACKAGE, version=VERSION)], previous_lock=previous_lock
    )


def _resolve_user(root: Path) -> ResolvedCapabilities:
    """Resolve the package from a user directory, which is never the reviewed base."""

    source = CapabilitySource(
        kind=SourceKind.USER, label="user-fixture", port=DirectoryCapabilitySource(root)
    )
    return CapabilityResolver([source]).resolve_with_files(
        [CapabilityRequest(name=PACKAGE, version=VERSION)]
    )


def _install(
    resolved: ResolvedCapabilities,
    tmp_path: Path,
    approvals: ApprovedCapabilities = NO_APPROVALS,
) -> tuple[Path, Path]:
    install_root, store = tmp_path / "installed", tmp_path / "store"
    install_lock(resolved, install_root=install_root, capabilities_root=store, approvals=approvals)
    return install_root, store


def _run(
    resolved: ResolvedCapabilities,
    install_root: Path,
    store: Path,
    approvals: ApprovedCapabilities = NO_APPROVALS,
) -> CommandResult:
    return run_package_command(
        resolved.lock,
        PACKAGE,
        {"text": "one two  three"},
        install_root=install_root,
        capabilities_root=store,
        approvals=approvals,
        interpreter_paths=INTERPRETERS,
    )


def _script(root: Path) -> Path:
    return root / PACKAGE / "scripts" / "count_words.py"


def _reference(root: Path) -> Path:
    return root / PACKAGE / "references" / "usage.md"


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` with its bytes, so a comparison sees a change, not just a loss."""

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_the_fixture_is_a_complete_reviewed_closure() -> None:
    closure = build_closure_from_directory(FIXTURE_ROOT / PACKAGE)

    assert closure.manifest.name == PACKAGE
    assert {record.kind for record in closure.files} == {
        AssetKind.METADATA,
        AssetKind.INSTRUCTION,
        AssetKind.REFERENCE,
        AssetKind.SCRIPT,
    }
    assert {record.path for record in closure.files} == _PACKAGE_FILES


def test_a_directory_can_never_be_the_project_source() -> None:
    with pytest.raises(InvalidInputError):
        CapabilitySource(
            kind=SourceKind.PROJECT,
            label="project-directory",
            port=DirectoryCapabilitySource(FIXTURE_ROOT),
        )


def test_the_committed_package_resolves_as_the_reviewed_base(reviewed: _Reviewed) -> None:
    resolved = _resolve_at(reviewed.repository, reviewed.commit)

    entry = resolved.lock.entries[0]
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.introduced_by_run is False
    assert entry.revision == reviewed.commit
    assert entry.digest == build_closure_from_directory(FIXTURE_ROOT / PACKAGE).digest
    assert {record.path for record in entry.files} == _PACKAGE_FILES


def test_a_project_fixture_installs_runs_and_uninstalls_without_approval(
    tmp_path: Path, reviewed: _Reviewed
) -> None:
    resolved = _resolve_at(reviewed.repository, reviewed.commit)
    entry = resolved.lock.entries[0]
    assert build_install_plan(resolved, install_root=tmp_path / "installed").ready

    install_root, store = _install(resolved, tmp_path)
    result = _run(resolved, install_root, store)

    assert result.output == {"words": 3}
    assert result.canonical_output == '{"words":3}'
    assert (install_root / PACKAGE / "scripts" / "count_words.py").is_file()
    lockfile = tmp_path / "capabilities.lock.json"
    write_lock(resolved.lock, lockfile)
    lockfile_before, store_before = lockfile.read_bytes(), _snapshot(store)

    report = uninstall_install(install_root)

    assert set(report.removed) == {f"{PACKAGE}/{path}" for path in _PACKAGE_FILES}
    assert not (install_root / OWNED_MANIFEST_NAME).exists()
    revocation = install_root / f"{REVOKED_RECORD_PREFIX}{resolved.lock.digest().hex_value}-1.json"
    assert report.revocation_record == revocation
    assert revocation.is_file()
    assert digest_path(store, entry.digest).is_dir()
    assert lockfile.read_bytes() == lockfile_before
    assert _snapshot(store) == store_before
    assert read_lock(lockfile).digest() == resolved.lock.digest()
    with pytest.raises(CapabilityNotInstalledError):
        _run(resolved, install_root, store)


def test_a_script_changed_in_a_later_commit_is_drift_and_the_pinned_commit_is_unaffected(
    reviewed: _Reviewed,
) -> None:
    original = _resolve_at(reviewed.repository, reviewed.commit)
    _append(_script(reviewed.repository / SUBDIR), "# changed\n")
    changed_commit = _commit_all(reviewed.repository, "change the script")

    with pytest.raises(CapabilityLockDriftError):
        _resolve_at(reviewed.repository, changed_commit, previous_lock=original.lock)
    changed = _resolve_at(reviewed.repository, changed_commit)
    assert changed.lock.entries[0].digest != original.lock.entries[0].digest
    assert changed.lock.digest() != original.lock.digest()
    pinned = _resolve_at(reviewed.repository, reviewed.commit, previous_lock=original.lock)
    assert pinned.lock.entries[0].digest == original.lock.entries[0].digest
    assert pinned.lock.digest() == original.lock.digest()


def test_a_reference_changed_in_a_later_commit_is_drift_and_changes_the_lock(
    reviewed: _Reviewed,
) -> None:
    original = _resolve_at(reviewed.repository, reviewed.commit)
    _append(_reference(reviewed.repository / SUBDIR), "\nExtra.\n")
    changed_commit = _commit_all(reviewed.repository, "change the reference")

    with pytest.raises(CapabilityLockDriftError):
        _resolve_at(reviewed.repository, changed_commit, previous_lock=original.lock)
    changed = _resolve_at(reviewed.repository, changed_commit)
    assert changed.lock.entries[0].digest != original.lock.entries[0].digest
    assert changed.lock.digest() != original.lock.digest()


def test_an_uncommitted_edit_is_never_seen_at_the_pinned_commit(reviewed: _Reviewed) -> None:
    original = _resolve_at(reviewed.repository, reviewed.commit)
    package = reviewed.repository / SUBDIR
    _append(_script(package), "# uncommitted\n")
    _append(_reference(package), "\nstaged but not committed\n")
    _git(reviewed.repository, "add", str(_reference(package)))
    (package / PACKAGE / "references" / "untracked.md").write_text("untracked", encoding="utf-8")

    pinned = _resolve_at(reviewed.repository, reviewed.commit, previous_lock=original.lock)

    assert pinned.lock.digest() == original.lock.digest()
    assert {record.path for record in pinned.lock.entries[0].files} == _PACKAGE_FILES


def test_a_script_tampered_in_the_materialized_store_is_never_executed(
    tmp_path: Path, reviewed: _Reviewed
) -> None:
    resolved = _resolve_at(reviewed.repository, reviewed.commit)
    install_root, store = _install(resolved, tmp_path)
    marker = tmp_path / "executed.marker"
    tampered = digest_path(store, resolved.lock.entries[0].digest) / "scripts" / "count_words.py"
    tampered.chmod(0o644)
    tampered.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\nprint('{{}}')\n",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityIntegrityError):
        _run(resolved, install_root, store)

    assert not marker.exists()


def test_a_reference_tampered_in_the_materialized_store_is_rejected(
    tmp_path: Path, reviewed: _Reviewed
) -> None:
    resolved = _resolve_at(reviewed.repository, reviewed.commit)
    install_root, store = _install(resolved, tmp_path)
    entry = resolved.lock.entries[0]
    reference = digest_path(store, entry.digest) / "references" / "usage.md"
    reference.chmod(0o644)
    reference.write_text("tampered", encoding="utf-8")

    with pytest.raises(CapabilityIntegrityError):
        verify_materialized(entry, store)
    with pytest.raises(CapabilityIntegrityError):
        _run(resolved, install_root, store)
    with pytest.raises(CapabilityIntegrityError):
        install_lock(resolved, install_root=install_root, capabilities_root=store)


def test_a_script_that_misbehaves_is_rejected_at_the_command_boundary(
    tmp_path: Path, reviewed: _Reviewed
) -> None:
    _script(reviewed.repository / SUBDIR).write_text("print('not json')\n", encoding="utf-8")
    misbehaving_commit = _commit_all(reviewed.repository, "break the script")
    resolved = _resolve_at(reviewed.repository, misbehaving_commit)
    install_root, store = _install(resolved, tmp_path)

    with pytest.raises(CapabilityCommandOutputError):
        _run(resolved, install_root, store)


def test_the_same_package_from_a_user_source_needs_exact_digest_approval(tmp_path: Path) -> None:
    resolved = _resolve_user(_copy_fixture(tmp_path))
    entry = resolved.lock.entries[0]
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.requires_human_approval()

    plan = build_install_plan(resolved, install_root=tmp_path / "installed")

    assert not plan.ready
    (approval,) = plan.approvals_required
    assert (approval.closure_digest, approval.lock_digest) == (entry.digest, resolved.lock.digest())
    with pytest.raises(CapabilityApprovalRequiredError):
        _install(resolved, tmp_path)
    assert not (tmp_path / "installed").exists()


def test_a_digest_bound_approval_lets_a_user_package_install_and_run(tmp_path: Path) -> None:
    resolved = _resolve_user(_copy_fixture(tmp_path))
    approvals = ApprovedCapabilities.for_entries(resolved.lock, [PACKAGE])

    install_root, store = _install(resolved, tmp_path, approvals)

    assert _run(resolved, install_root, store, approvals).output == {"words": 3}
    with pytest.raises(CapabilityApprovalRequiredError):
        _run(resolved, install_root, store)


def test_an_unknown_license_holds_a_user_package_even_when_approved(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    manifest_path = root / PACKAGE / "capability.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["license"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    resolved = _resolve_user(root)

    with pytest.raises(CapabilityLicenseHoldError):
        install_lock(
            resolved,
            install_root=tmp_path / "installed",
            capabilities_root=tmp_path / "store",
            approvals=ApprovedCapabilities.for_entries(resolved.lock, [PACKAGE]),
        )

    assert not (tmp_path / "store").exists()


def test_an_approval_is_superseded_when_a_sibling_script_changes(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    first = _resolve_user(root)
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("fixture-policy", approval_registry=registry)
    registry.record_approval(
        materialize_approval_schema(
            first.lock.entries[0], first.lock, requesting_principal_id=_RUN.id
        ),
        [_OPERATOR],
    )
    approved = approved_capabilities(first.lock, evaluator, requesting_principal_id=_RUN.id)
    assert approved == ApprovedCapabilities.for_entries(first.lock, [PACKAGE])
    _append(_script(root), "# changed after approval\n")
    drifted = _resolve_user(root)

    superseded = approved_capabilities(drifted.lock, evaluator, requesting_principal_id=_RUN.id)

    assert superseded == NO_APPROVALS
    for approvals in (superseded, approved):
        with pytest.raises(CapabilityApprovalRequiredError):
            install_lock(
                drifted,
                install_root=tmp_path / "installed",
                capabilities_root=tmp_path / "store",
                approvals=approvals,
            )
    assert not (tmp_path / "installed").exists()


def test_uninstall_retains_evidence_ledger_store_lockfile_and_modified_files(
    tmp_path: Path, reviewed: _Reviewed
) -> None:
    resolved = _resolve_at(reviewed.repository, reviewed.commit)
    state = tmp_path / ".enginery"
    install_root, store = state / "installed", state / "capabilities"
    install_lock(resolved, install_root=install_root, capabilities_root=store)
    lockfile = state / "capabilities.lock.json"
    write_lock(resolved.lock, lockfile)
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"ledger bytes")
    evidence = tmp_path / "evidence" / "raw.bin"
    evidence.parent.mkdir()
    evidence.write_bytes(b"raw evidence")
    edited = install_root / PACKAGE / "references" / "usage.md"
    edited.chmod(0o644)
    edited.write_text("edited locally", encoding="utf-8")
    lockfile_before, store_before = lockfile.read_bytes(), _snapshot(store)

    report = uninstall_install(install_root)

    assert ledger.read_bytes() == b"ledger bytes"
    assert evidence.read_bytes() == b"raw evidence"
    assert lockfile.read_bytes() == lockfile_before
    assert _snapshot(store) == store_before
    assert edited.read_text(encoding="utf-8") == "edited locally"
    assert [(item.path, item.reason) for item in report.retained] == [
        (f"{PACKAGE}/references/usage.md", "modified after install")
    ]


def test_the_documented_operator_smoke_runs_through_the_cli(
    tmp_path: Path,
    reviewed: _Reviewed,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    request = f"{PACKAGE}@{VERSION}"

    def pinned_to(commit: str) -> list[str]:
        return ["--project-source", f"{reviewed.repository}@{commit}", "--project-subdir", SUBDIR]

    def cli(*arguments: str) -> tuple[int, str]:
        capsys.readouterr()
        code = main(["capability", *arguments])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    base = pinned_to(reviewed.commit)
    code, output = cli("inspect", request, *base)
    assert code == 0
    assert "local_trusted" in output
    assert not (project / ".enginery").exists()

    code, output = cli("preview", request, *base)
    assert code == 0
    assert f"write {PACKAGE}/scripts/count_words.py" in output
    assert not (project / ".enginery").exists()

    code, _ = cli("install", request, *base)
    assert code == 0
    installed = project / ".enginery" / "installed" / PACKAGE / "scripts" / "count_words.py"
    assert installed.is_file()
    installed_bytes = installed.read_bytes()
    code, output = cli("lock", "--check")
    assert code == 0
    assert "no drift" in output

    # An edit that is not committed is not part of the pinned commit, whatever the files say.
    _append(_script(reviewed.repository / SUBDIR), "# changed\n")
    code, _ = cli("preview", request, *base)
    assert code == 0
    code, output = cli("install", request, *base)
    assert code == 0
    assert "already installed" in output
    assert installed.read_bytes() == installed_bytes

    # The same edit, committed, is a different closure under the same name and version.
    changed = pinned_to(_commit_all(reviewed.repository, "change the script"))
    code, output = cli("preview", request, *changed)
    assert code == 3
    assert "different closure digest" in output
    code, output = cli("install", request, *changed)
    assert code == 3
    assert installed.read_bytes() == installed_bytes
    assert "# changed" not in installed.read_text(encoding="utf-8")
    code, _ = cli("preview", request, *base)
    assert code == 0

    code, output = cli("uninstall", "--json")
    assert code == 0
    document: dict[str, Any] = json.loads(output)
    assert f"{PACKAGE}/scripts/count_words.py" in document["removed"]
    retained = {item["path"] for item in document["retained"]}
    assert {".enginery/capabilities", ".enginery/capabilities.lock.json"} <= retained
    assert (project / ".enginery" / "capabilities").is_dir()
    assert (project / ".enginery" / "capabilities.lock.json").is_file()
    assert not installed.exists()
    lock_hex = str(document["lock_digest"]).removeprefix("sha256:")
    revocation = Path(document["revocation_record"])
    assert revocation.name == f"{REVOKED_RECORD_PREFIX}{lock_hex}-1.json"
    assert revocation.is_file()

    # A user directory is never the reviewed base: in a fresh workspace the same package is held.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    user_packages = _copy_fixture(tmp_path)
    closure_digest = build_closure_from_directory(FIXTURE_ROOT / PACKAGE).digest
    code, output = cli("install", request, "--user-source", str(user_packages))
    assert code == 5
    assert f"HOLD {request}: closure {closure_digest} lock " in output
    assert not (fresh / ".enginery").exists()
