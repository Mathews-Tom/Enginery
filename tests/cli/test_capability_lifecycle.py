from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.application.git_environment import repository_isolated_environment
from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.domain.errors import FailureClass
from tests.support.capabilities import package_files


def _write_package(root: Path, files: Sequence[PackageFile], name: str = "skill-a") -> Path:
    directory = root / name
    for item in files:
        target = directory / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)
        if item.executable:
            target.chmod(0o755)
    return directory


class _Paths:
    def __init__(self, base: Path) -> None:
        self.project = base / "project-packages"
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


def _project_flags(paths: _Paths) -> list[str]:
    return ["--project-source", str(paths.project), *paths.flags(), "--json"]


def test_inspect_reports_closure_trust_and_approval_without_side_effects(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_package(paths.project, package_files("skill-a"))

    code, document, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths))

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
    _write_package(paths.project, package_files("skill-a"))

    code, document, _ = _run(capsys, "preview", "skill-a@1", *_project_flags(paths))

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


def test_preview_reports_the_shadowed_alternative_for_a_lower_precedence_source(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_package(paths.project, package_files("skill-a"))
    _write_package(paths.user, package_files("skill-a", body=b"# another body\n"))

    code, document, _ = _run(
        capsys, "preview", "skill-a@1", "--user-source", str(paths.user), *_project_flags(paths)
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
    _write_package(paths.project, package_files("skill-a"))

    code, document, _ = _run(capsys, "install", "skill-a@1", *_project_flags(paths))

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
    _write_package(paths.project, files, "tool")
    code, _, _ = _run(capsys, "install", "tool@1", *_project_flags(paths))
    assert code == 0
    installed_before = sorted(path.name for path in (paths.installed / "tool").rglob("*"))
    (paths.project / "tool" / "scripts" / "helper.py").write_bytes(b"print('tampered')\n")

    code, _, output = _run(capsys, "preview", "tool@1", *_project_flags(paths))

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert "different closure digest" in output
    code, _, output = _run(capsys, "install", "tool@1", *_project_flags(paths))
    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert (paths.installed / "tool" / "scripts" / "helper.py").read_bytes() == b"print('v1')\n"
    assert installed_before == sorted(path.name for path in (paths.installed / "tool").rglob("*"))


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
    code, _, output = _run(capsys, "inspect", "skill-a", *_project_flags(paths))

    assert code == exit_code_for(FailureClass.INVALID_INPUT)
    assert "NAME@VERSION" in output


def test_an_unknown_capability_is_a_missing_prerequisite(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    paths.project.mkdir(parents=True)

    code, _, _ = _run(capsys, "inspect", "skill-a@1", *_project_flags(paths))

    assert code == exit_code_for(FailureClass.MISSING_PREREQUISITE)


def test_a_package_with_a_symlink_is_rejected_by_the_cli(
    paths: _Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _write_package(paths.project, package_files("skill-a"))
    secret = paths.project.parent / "secret.md"
    secret.write_bytes(b"outside the package\n")
    (directory / "references" / "escape.md").symlink_to(secret)

    code, _, output = _run(capsys, "preview", "skill-a@1", *_project_flags(paths))

    assert code == exit_code_for(FailureClass.VALIDATION_FAILURE)
    assert "symlink" in output
    assert not paths.installed.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_a_git_source_pinned_to_a_commit_is_held_for_approval(
    paths: _Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "repository"
    _write_package(repository / "packages", package_files("skill-a"))
    environment = {
        **repository_isolated_environment(),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "add package"],
    ):
        subprocess.run(command, cwd=repository, env=environment, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    code, document, _ = _run(
        capsys,
        "install",
        "skill-a@1",
        "--git-source",
        f"{repository}@{sha}",
        "--git-subdir",
        "packages",
        *paths.flags(),
        "--json",
    )

    assert code == exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    assert document["status"] == "hold"
    assert not paths.installed.exists()
