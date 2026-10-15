"""End-to-end lifecycle of the committed reviewed fixture package.

The fixture is a real multi-file package (manifest, instruction, reference, script) used as a
project package. Every test goes through actual resolution, materialization, install, command
execution, and uninstall; nothing here reaches the network.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.directory_capability import DirectoryCapabilitySource
from enginery.capabilities.approval import approved_capability_names, materialize_approval_schema
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
    REVOKED_RECORD_NAME,
    build_install_plan,
    install_lock,
    uninstall_install,
)
from enginery.capabilities.lock import CapabilityLock, ProvenanceStatus
from enginery.capabilities.materialize import digest_path, verify_materialized
from enginery.capabilities.resolver import (
    CapabilityRequest,
    CapabilityResolver,
    ResolvedCapabilities,
)
from enginery.capabilities.serialization import read_lock, write_lock
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.cli.main import main
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "capabilities" / "project"
PACKAGE = "word-counter"
VERSION = "1.0.0"
INTERPRETERS = {"python3": sys.executable}
_OPERATOR = AuthorityPrincipal(
    id="operator-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_RUN = AuthorityPrincipal(
    id="run-1", principal_type=PrincipalType.AGENT, role="run", authorization_source="coordinator"
)


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "packages"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def _resolve(
    root: Path,
    kind: SourceKind = SourceKind.PROJECT,
    previous_lock: CapabilityLock | None = None,
) -> ResolvedCapabilities:
    source = CapabilitySource(
        kind=kind, label=f"{kind.value}-fixture", port=DirectoryCapabilitySource(root)
    )
    return CapabilityResolver([source]).resolve_with_files(
        [CapabilityRequest(name=PACKAGE, version=VERSION)], previous_lock=previous_lock
    )


def _install(resolved: ResolvedCapabilities, tmp_path: Path) -> tuple[Path, Path]:
    install_root, store = tmp_path / "installed", tmp_path / "store"
    install_lock(resolved, install_root=install_root, capabilities_root=store)
    return install_root, store


def _run(resolved: ResolvedCapabilities, install_root: Path, store: Path) -> CommandResult:
    return run_package_command(
        resolved.lock,
        PACKAGE,
        {"text": "one two  three"},
        install_root=install_root,
        capabilities_root=store,
        interpreter_paths=INTERPRETERS,
    )


def _script(root: Path) -> Path:
    return root / PACKAGE / "scripts" / "count_words.py"


def test_the_fixture_is_a_complete_reviewed_closure() -> None:
    closure = build_closure_from_directory(FIXTURE_ROOT / PACKAGE)

    assert closure.manifest.name == PACKAGE
    assert {record.kind for record in closure.files} == {
        AssetKind.METADATA,
        AssetKind.INSTRUCTION,
        AssetKind.REFERENCE,
        AssetKind.SCRIPT,
    }
    assert {record.path for record in closure.files} == {
        "SKILL.md",
        "capability.json",
        "references/usage.md",
        "scripts/count_words.py",
    }


def test_a_project_fixture_installs_runs_and_uninstalls_without_approval(tmp_path: Path) -> None:
    resolved = _resolve(FIXTURE_ROOT)
    entry = resolved.lock.entries[0]
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.introduced_by_run is False
    assert build_install_plan(resolved, install_root=tmp_path / "installed").ready

    install_root, store = _install(resolved, tmp_path)
    result = _run(resolved, install_root, store)

    assert result.output == {"words": 3}
    assert result.canonical_output == '{"words":3}'
    assert (install_root / PACKAGE / "scripts" / "count_words.py").is_file()
    lockfile = tmp_path / "capabilities.lock.json"
    write_lock(resolved.lock, lockfile)

    report = uninstall_install(install_root)

    assert set(report.removed) == {
        f"{PACKAGE}/SKILL.md",
        f"{PACKAGE}/capability.json",
        f"{PACKAGE}/references/usage.md",
        f"{PACKAGE}/scripts/count_words.py",
    }
    assert not (install_root / OWNED_MANIFEST_NAME).exists()
    assert (install_root / REVOKED_RECORD_NAME).is_file()
    assert digest_path(store, entry.digest).is_dir()
    assert read_lock(lockfile).digest() == resolved.lock.digest()
    with pytest.raises(CapabilityNotInstalledError):
        _run(resolved, install_root, store)


def test_a_changed_sibling_script_changes_the_lock_and_is_rejected_as_drift(
    tmp_path: Path,
) -> None:
    root = _copy_fixture(tmp_path)
    resolved = _resolve(root)
    _script(root).write_text(
        _script(root).read_text(encoding="utf-8") + "# changed\n", encoding="utf-8"
    )

    with pytest.raises(CapabilityLockDriftError):
        _resolve(root, previous_lock=resolved.lock)

    changed = _resolve(root)
    assert changed.lock.entries[0].digest != resolved.lock.entries[0].digest
    assert changed.lock.digest() != resolved.lock.digest()


def test_a_changed_reference_changes_the_closure_digest(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    original = _resolve(root).lock.entries[0].digest
    reference = root / PACKAGE / "references" / "usage.md"
    reference.write_text(reference.read_text(encoding="utf-8") + "\nExtra.\n", encoding="utf-8")

    assert _resolve(root).lock.entries[0].digest != original


def test_a_script_tampered_in_the_materialized_store_is_never_executed(tmp_path: Path) -> None:
    resolved = _resolve(FIXTURE_ROOT)
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


def test_a_reference_tampered_in_the_materialized_store_is_rejected(tmp_path: Path) -> None:
    resolved = _resolve(FIXTURE_ROOT)
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


def test_a_script_that_misbehaves_is_rejected_at_the_command_boundary(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    _script(root).write_text("print('not json')\n", encoding="utf-8")
    resolved = _resolve(root)
    install_root, store = _install(resolved, tmp_path)

    with pytest.raises(CapabilityCommandOutputError):
        _run(resolved, install_root, store)


def test_the_same_package_from_a_user_source_needs_exact_digest_approval(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    resolved = _resolve(root, SourceKind.USER)
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


def test_an_unknown_license_holds_a_user_package_even_when_approved(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    manifest_path = root / PACKAGE / "capability.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["license"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    resolved = _resolve(root, SourceKind.USER)

    with pytest.raises(CapabilityLicenseHoldError):
        install_lock(
            resolved,
            install_root=tmp_path / "installed",
            capabilities_root=tmp_path / "store",
            approved_names=frozenset({PACKAGE}),
        )

    assert not (tmp_path / "store").exists()


def test_an_approval_is_superseded_when_a_sibling_script_changes(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    reviewed = _resolve(root, SourceKind.USER)
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("fixture-policy", approval_registry=registry)
    registry.record_approval(
        materialize_approval_schema(
            reviewed.lock.entries[0], reviewed.lock, requesting_principal_id=_RUN.id
        ),
        [_OPERATOR],
    )
    assert approved_capability_names(
        reviewed.lock, evaluator, requesting_principal_id=_RUN.id
    ) == frozenset({PACKAGE})
    _script(root).write_text(
        _script(root).read_text(encoding="utf-8") + "# changed after approval\n", encoding="utf-8"
    )
    drifted = _resolve(root, SourceKind.USER)

    approved = approved_capability_names(drifted.lock, evaluator, requesting_principal_id=_RUN.id)

    assert approved == frozenset()
    with pytest.raises(CapabilityApprovalRequiredError):
        install_lock(
            drifted,
            install_root=tmp_path / "installed",
            capabilities_root=tmp_path / "store",
            approved_names=approved,
        )


def test_uninstall_retains_evidence_ledger_store_lockfile_and_modified_files(
    tmp_path: Path,
) -> None:
    resolved = _resolve(FIXTURE_ROOT)
    install_root, store = _install(resolved, tmp_path)
    lockfile = tmp_path / "capabilities.lock.json"
    write_lock(resolved.lock, lockfile)
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"ledger bytes")
    evidence = tmp_path / "evidence" / "raw.bin"
    evidence.parent.mkdir()
    evidence.write_bytes(b"raw evidence")
    edited = install_root / PACKAGE / "references" / "usage.md"
    edited.chmod(0o644)
    edited.write_text("edited locally", encoding="utf-8")

    report = uninstall_install(install_root)

    assert ledger.read_bytes() == b"ledger bytes"
    assert evidence.read_bytes() == b"raw evidence"
    assert lockfile.is_file()
    assert digest_path(store, resolved.lock.entries[0].digest).is_dir()
    assert edited.read_text(encoding="utf-8") == "edited locally"
    assert [(item.path, item.reason) for item in report.retained] == [
        (f"{PACKAGE}/references/usage.md", "modified after install")
    ]


def test_the_documented_operator_smoke_runs_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE_ROOT, project / "capability-packages")
    monkeypatch.chdir(project)
    packages = ["--project-source", "capability-packages"]
    request = f"{PACKAGE}@{VERSION}"

    def cli(*arguments: str) -> tuple[int, str]:
        capsys.readouterr()
        code = main(["capability", *arguments])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    code, output = cli("inspect", request, *packages)
    assert code == 0
    assert "local_trusted" in output
    assert not (project / ".enginery").exists()

    code, output = cli("preview", request, *packages)
    assert code == 0
    assert f"write {PACKAGE}/scripts/count_words.py" in output
    assert not (project / ".enginery").exists()

    code, _ = cli("install", request, *packages)
    assert code == 0
    installed = project / ".enginery" / "installed" / PACKAGE
    assert (installed / "scripts" / "count_words.py").is_file()
    code, _ = cli("lock", "--check")
    assert code == 0

    source_script = _script(project / "capability-packages")
    source_script.write_text(source_script.read_text(encoding="utf-8") + "# drift\n", "utf-8")
    code, output = cli("preview", request, *packages)
    assert code != 0
    assert "different closure digest" in output
    code, _ = cli("install", request, *packages)
    assert code != 0
    assert "# drift" not in (installed / "scripts" / "count_words.py").read_text(encoding="utf-8")

    code, output = cli("uninstall", "--json")
    assert code == 0
    document: dict[str, Any] = json.loads(output)
    assert f"{PACKAGE}/scripts/count_words.py" in document["removed"]
    retained = {item["path"] for item in document["retained"]}
    assert {".enginery/capabilities", ".enginery/capabilities.lock.json"} <= retained
    assert (project / ".enginery" / "capabilities").is_dir()
    assert (project / ".enginery" / "capabilities.lock.json").is_file()
    assert not (installed / "scripts" / "count_words.py").exists()
