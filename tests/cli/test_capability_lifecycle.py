from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.application.git_environment import repository_isolated_environment
from enginery.capabilities.closure import build_closure
from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass
from tests.support.capabilities import NOW, package_files

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

_NOT_A_COMMIT = "a" * 40


def _write_package(root: Path, files: Sequence[PackageFile], name: str = "skill-a") -> Path:
    directory = root / name
    for item in files:
        target = directory / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)
        if item.executable:
            target.chmod(0o755)
    return directory


def _git_environment() -> dict[str, str]:
    return {
        **repository_isolated_environment(),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
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


def _commit(repository: Path, message: str = "commit") -> str:
    """Commit everything under ``repository`` and return the new commit sha."""

    if not (repository / ".git").exists():
        repository.mkdir(parents=True, exist_ok=True)
        _git(repository, "init", "-q", "-b", "main")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


class _Paths:
    def __init__(self, base: Path) -> None:
        self.repository = base / "project-repository"
        self.project = self.repository / "packages"
        self.user = base / "user-packages"
        self.lockfile = base / "state" / "capabilities.lock.json"
        self.store = base / "state" / "capabilities"
        self.installed = base / "state" / "installed"

    def flags(self) -> list[str]:
        return [
            "--lockfile",
            str(self.lockfile),
            "--capabilities-root",
            str(self.store),
            "--install-root",
            str(self.installed),
        ]


@pytest.fixture
def paths(tmp_path: Path) -> _Paths:
    return _Paths(tmp_path)


def _run(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, Any], str]:
    capsys.readouterr()
    code = main(["capability", *arguments])
    captured = capsys.readouterr()
    document: dict[str, Any] = {}
    if captured.out.strip().startswith("{"):
        document = json.loads(captured.out)
    return code, document, captured.out + captured.err


def _project_flags(paths: _Paths, commit: str) -> list[str]:
    return [
        "--project-source",
        f"{paths.repository}@{commit}",
        "--project-subdir",
        "packages",
        *paths.flags(),
        "--json",
    ]


def _commit_project(paths: _Paths, files: Sequence[PackageFile], name: str = "skill-a") -> str:
    _write_package(paths.project, files, name)
    return _commit(paths.repository)


def test_inspect_reports_closure_trust_and_approval_without_side_effects(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))

    code, document, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths, sha))

    assert code == 0
    packages = document["packages"]
    assert isinstance(packages, list)
    (package,) = packages
    assert package["trust"] == "local_trusted"
    assert package["approval_required"] is False
    assert package["closure_digest"].startswith("sha256:")
    assert package["license"] == "MIT"
    assert not paths.store.exists()
    assert not paths.installed.exists()
    assert not paths.lockfile.exists()


def test_preview_lists_the_footprint_and_writes_nothing(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))

    code, document, _ = _run(capsys, "preview", "skill-a@1", *_project_flags(paths, sha))

    assert code == 0
    assert document["ready"] is True
    packages = document["packages"]
    assert isinstance(packages, list)
    destinations = {item["destination"] for item in packages[0]["files"]}
    assert destinations == {
        "skill-a/SKILL.md",
        "skill-a/capability.json",
        "skill-a/references/guide.md",
    }
    assert not paths.store.exists()
    assert not paths.installed.exists()


def test_preview_of_a_url_git_source_leaves_no_files_behind(
    paths: _Paths,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))
    working = tmp_path / "working"
    scratch = tmp_path / "scratch"
    working.mkdir()
    scratch.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    source = f"{paths.repository.as_uri()}@{sha}"

    for command in ("preview", "inspect"):
        code, _, _ = _run(
            capsys,
            command,
            "skill-a@1",
            "--git-source",
            source,
            "--git-subdir",
            "packages",
            *paths.flags(),
            "--json",
        )
        assert code in (0, exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED))

    assert list(working.rglob("*")) == []
    assert list(scratch.iterdir()) == []


def test_preview_reports_the_shadowed_alternative_for_a_lower_precedence_source(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))
    _write_package(paths.user, package_files("skill-a", body=b"# another body\n"))

    code, document, _ = _run(
        capsys,
        "preview",
        "skill-a@1",
        "--user-source",
        str(paths.user),
        *_project_flags(paths, sha),
    )

    assert code == 0
    packages = document["packages"]
    assert isinstance(packages, list)
    source = packages[0]["source"]
    assert source["selected"]["kind"] == "project"
    assert [item["kind"] for item in source["shadowed"]] == ["user"]


def test_install_then_lock_check_then_uninstall_retains_store_and_lockfile(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))

    code, document, _ = _run(capsys, "install", "skill-a@1", *_project_flags(paths, sha))

    assert code == 0
    assert document["status"] == "installed"
    assert (paths.installed / "skill-a" / "SKILL.md").is_file()
    assert (paths.installed / ".enginery-install.json").is_file()
    assert paths.lockfile.is_file()
    code, _, _ = _run(capsys, "lock", "--check", *paths.flags()[:4])
    assert code == 0

    code, document, _ = _run(capsys, "uninstall", *paths.flags(), "--json")

    assert code == 0
    assert set(document["removed"]) == {
        "skill-a/SKILL.md",
        "skill-a/capability.json",
        "skill-a/references/guide.md",
    }
    retained = {item["path"] for item in document["retained"]}
    assert str(paths.store) in retained
    assert str(paths.lockfile) in retained
    assert not (paths.installed / "skill-a" / "SKILL.md").exists()
    assert paths.store.is_dir()
    assert paths.lockfile.is_file()


def test_install_holds_an_unapproved_entry_and_prints_the_exact_digests(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_package(paths.user, package_files("skill-a"))
    flags = ["--user-source", str(paths.user), *paths.flags()]

    code, document, _ = _run(capsys, "preview", "skill-a@1", *flags, "--json")
    assert code == exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    approvals = document["approvals_required"]
    assert isinstance(approvals, list)
    (approval,) = approvals

    code, held, _ = _run(capsys, "install", "skill-a@1", *flags, "--json")

    assert code == exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    assert held["status"] == "hold"
    assert held["approvals_required"] == [approval]
    assert approval["name"] == "skill-a"
    assert approval["version"] == "1"
    assert approval["closure_digest"].startswith("sha256:")
    assert approval["lock_digest"] == held["lock_digest"]
    assert not paths.installed.exists()
    assert not paths.store.exists()
    assert not paths.lockfile.exists()


def test_install_hold_text_output_names_the_digests(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_package(paths.user, package_files("skill-a"))

    code, _, output = _run(
        capsys, "install", "skill-a@1", "--user-source", str(paths.user), *paths.flags()
    )

    assert code == exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    assert "HOLD skill-a@1" in output
    assert "closure sha256:" in output
    assert "lock sha256:" in output
    assert not paths.installed.exists()


def test_a_license_hold_is_not_an_approval_hold(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_package(paths.user, package_files("skill-a", license_id=None))

    code, _, output = _run(
        capsys, "install", "skill-a@1", "--user-source", str(paths.user), *paths.flags()
    )

    assert code == exit_code_for(FailureClass.VALIDATION_FAILURE)
    assert "license" in output
    assert not paths.installed.exists()


def test_a_changed_referenced_script_is_rejected_as_drift_against_the_lockfile(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    files = package_files("tool", extra={"scripts/helper.py": b"print('v1')\n"})
    first = _commit_project(paths, files, "tool")
    code, _, _ = _run(capsys, "install", "tool@1", *_project_flags(paths, first))
    assert code == 0
    installed_before = sorted(path.name for path in (paths.installed / "tool").rglob("*"))
    (paths.project / "tool" / "scripts" / "helper.py").write_bytes(b"print('tampered')\n")
    second = _commit(paths.repository, "change the helper")

    code, _, output = _run(capsys, "preview", "tool@1", *_project_flags(paths, second))

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "different closure digest" in output
    code, _, output = _run(capsys, "install", "tool@1", *_project_flags(paths, second))
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert (paths.installed / "tool" / "scripts" / "helper.py").read_bytes() == b"print('v1')\n"
    assert installed_before == sorted(path.name for path in (paths.installed / "tool").rglob("*"))


def test_install_keeps_earlier_pins_so_a_changed_closure_is_still_drift(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_package(paths.project, package_files("skill-a"), "skill-a")
    first = _commit_project(paths, package_files("skill-b"), "skill-b")
    assert _run(capsys, "install", "skill-a@1", *_project_flags(paths, first))[0] == 0
    assert _run(capsys, "uninstall", *paths.flags(), "--json")[0] == 0
    assert _run(capsys, "install", "skill-b@1", *_project_flags(paths, first))[0] == 0
    assert _run(capsys, "uninstall", *paths.flags(), "--json")[0] == 0
    pinned = json.loads(paths.lockfile.read_text(encoding="utf-8"))
    assert {entry["name"] for entry in pinned["entries"]} == {"skill-a", "skill-b"}
    second = _commit_project(paths, package_files("skill-a", body=b"# changed closure\n"))

    code, _, output = _run(capsys, "install", "skill-a@1", *_project_flags(paths, second))

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "different closure digest" in output
    assert not (paths.installed / "skill-a" / "SKILL.md").exists()


def test_install_refuses_a_different_version_of_an_already_pinned_name(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _commit_project(paths, package_files("skill-a", "1"))
    assert _run(capsys, "install", "skill-a@1", *_project_flags(paths, first))[0] == 0
    assert _run(capsys, "uninstall", *paths.flags(), "--json")[0] == 0
    before = paths.lockfile.read_bytes()
    second = _commit_project(paths, package_files("skill-a", "2"))

    code, _, output = _run(capsys, "install", "skill-a@2", *_project_flags(paths, second))

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "different version" in output
    assert paths.lockfile.read_bytes() == before
    assert not (paths.installed / "skill-a" / "SKILL.md").exists()


def test_uninstall_without_an_install_is_a_missing_prerequisite(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, output = _run(capsys, "uninstall", *paths.flags(), "--json")

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "no active install" in output


def test_a_source_is_required(paths: _Paths, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, output = _run(capsys, "inspect", "skill-a@1", *paths.flags())

    assert code == exit_code_for(FailureClass.INVALID_INPUT)
    assert "at least one source" in output


def test_a_request_must_name_a_version(paths: _Paths, capsys: pytest.CaptureFixture[str]) -> None:
    sha = _commit_project(paths, package_files("skill-a"))

    code, _, output = _run(capsys, "inspect", "skill-a", *_project_flags(paths, sha))

    assert code == exit_code_for(FailureClass.INVALID_INPUT)
    assert "NAME@VERSION" in output


def test_an_unknown_capability_is_a_missing_prerequisite(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.project.mkdir(parents=True)
    (paths.project / "README.md").write_text("no packages here\n", encoding="utf-8")
    sha = _commit(paths.repository)

    code, _, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths, sha))

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)


def test_a_package_with_a_symlink_is_rejected_by_the_cli(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _write_package(paths.project, package_files("skill-a"))
    secret = paths.repository / "secret.md"
    secret.write_bytes(b"outside the package\n")
    (directory / "references" / "escape.md").symlink_to(secret)
    sha = _commit(paths.repository)

    code, _, output = _run(capsys, "preview", "skill-a@1", *_project_flags(paths, sha))

    assert code == exit_code_for(FailureClass.VALIDATION_FAILURE)
    assert "symlink" in output
    assert not paths.installed.exists()


# --------------------------------------------------------------------------- project source


def test_a_plain_directory_is_refused_as_the_project_source(
    paths: _Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "just-a-directory"
    _write_package(directory, package_files("skill-a"))

    code, _, output = _run(
        capsys, "install", "skill-a@1", "--project-source", str(directory), *paths.flags()
    )

    assert code == exit_code_for(FailureClass.INVALID_INPUT)
    assert "REPOSITORY@COMMIT" in output
    assert not paths.installed.exists()
    assert not paths.lockfile.exists()


def test_a_project_commit_that_is_not_in_a_repository_is_refused(
    paths: _Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "just-a-directory"
    _write_package(directory, package_files("skill-a"))

    code, _, _ = _run(
        capsys,
        "install",
        "skill-a@1",
        "--project-source",
        f"{directory}@{_NOT_A_COMMIT}",
        *paths.flags(),
    )

    assert code != 0
    assert not paths.installed.exists()
    assert not paths.lockfile.exists()


def test_a_committed_package_at_the_pinned_commit_is_local_trusted_at_that_revision(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))

    code, document, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths, sha))

    assert code == 0
    (package,) = document["packages"]
    assert package["trust"] == "local_trusted"
    assert package["approval_required"] is False
    assert package["introduced_by_run"] is False
    assert package["revision"] == sha
    assert package["source"]["selected"]["label"] == f"project:{paths.repository}"


def test_an_uncommitted_edit_is_not_part_of_the_reviewed_base(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))
    committed = build_closure(package_files("skill-a")).digest
    (paths.project / "skill-a" / "SKILL.md").write_bytes(b"# edited but never committed\n")
    _write_package(paths.project, package_files("skill-b"), "skill-b")

    code, document, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths, sha))

    assert code == 0
    assert document["packages"][0]["closure_digest"] == str(committed)
    code, _, _ = _run(capsys, "inspect", "skill-b@1", *_project_flags(paths, sha))
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    code, _, _ = _run(capsys, "install", "skill-a@1", *_project_flags(paths, sha))
    assert code == 0
    installed = (paths.installed / "skill-a" / "SKILL.md").read_bytes()
    assert installed == b"# skill instructions\n"


def test_a_later_commit_is_not_seen_at_the_pinned_commit(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _commit_project(paths, package_files("skill-a"))
    second = _commit_project(paths, package_files("skill-a", body=b"# changed in a later commit\n"))

    _, at_first, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths, first))
    _, at_second, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths, second))

    original = str(build_closure(package_files("skill-a")).digest)
    assert at_first["packages"][0]["closure_digest"] == original
    assert at_second["packages"][0]["closure_digest"] != original
    assert at_second["packages"][0]["revision"] == second


# --------------------------------------------------------------------------- legacy lockfile


def _write_legacy_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "name": "skill-a",
                        "version": "1",
                        "digest": str(Digest.of_bytes(b"blob")),
                        "license": "MIT",
                        "introduced_by_run": False,
                        "provenance": {
                            "status": "local_trusted",
                            "source_label": "local",
                            "signer_key_id": None,
                            "verified_at": NOW.isoformat(),
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_a_legacy_lockfile_is_refused_and_names_the_explicit_replacement_flag(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))
    _write_legacy_lock(paths.lockfile)
    legacy = paths.lockfile.read_bytes()

    for command in ("inspect", "preview", "install"):
        code, _, output = _run(capsys, command, "skill-a@1", *_project_flags(paths, sha))
        assert code == exit_code_for(FailureClass.VALIDATION_FAILURE)
        assert "--replace-legacy-lock" in output

    assert paths.lockfile.read_bytes() == legacy
    assert not paths.installed.exists()


def test_replace_legacy_lock_resolves_fresh_and_install_replaces_the_legacy_file(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))
    _write_legacy_lock(paths.lockfile)
    legacy = paths.lockfile.read_bytes()
    flags = [*_project_flags(paths, sha), "--replace-legacy-lock"]

    for command in ("inspect", "preview"):
        assert _run(capsys, command, "skill-a@1", *flags)[0] == 0
    assert paths.lockfile.read_bytes() == legacy

    code, _, _ = _run(capsys, "install", "skill-a@1", *flags)

    assert code == 0
    replaced = json.loads(paths.lockfile.read_text(encoding="utf-8"))
    assert replaced["schema_version"] == 2
    assert [entry["name"] for entry in replaced["entries"]] == ["skill-a"]
    assert (paths.installed / "skill-a" / "SKILL.md").is_file()


# --------------------------------------------------------------------------- git source


def test_a_git_source_pinned_to_a_commit_is_held_for_approval(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit_project(paths, package_files("skill-a"))

    code, document, _ = _run(
        capsys,
        "install",
        "skill-a@1",
        "--git-source",
        f"{paths.repository}@{sha}",
        "--git-subdir",
        "packages",
        *paths.flags(),
        "--json",
    )

    assert code == exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    assert document["status"] == "hold"
    assert not paths.installed.exists()
