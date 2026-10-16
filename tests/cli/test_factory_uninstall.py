from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from enginery.cli._exit_codes import exit_code_for
from enginery.cli.config_locations import private_state_directory
from enginery.cli.main import main
from enginery.domain.errors import FailureClass

INVALID = exit_code_for(FailureClass.INVALID_INPUT)


def _tree(*roots: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for root in roots:
        if root.exists():
            for path in sorted(root.rglob("*")):
                snapshot[str(path)] = (
                    "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
                )
    return snapshot


def _roots(project: Path) -> tuple[Path, Path, Path]:
    return project, Path(os.environ["XDG_CONFIG_HOME"]), Path(os.environ["XDG_STATE_HOME"])


def _factory(
    capsys: pytest.CaptureFixture[str], project: Path, command: str, *extra: str
) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = main(["factory", command, "--project", str(project), "--json", *extra])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def setup(project: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    capsys.readouterr()
    assert (
        main(["factory", "init", "--project", str(project), "--runner", "trusted-local", "--apply"])
        == 0
    )
    return project.resolve()


def _record_path(project: Path) -> Path:
    return private_state_directory(project.resolve()) / "setup-ownership.json"


def _bystanders(project: Path) -> dict[str, bytes]:
    """Everything uninstall must never touch, created beside what setup made."""
    files = {
        project / ".enginery" / "capabilities.lock.json": b'{"entries": []}',
        project / ".enginery" / "capabilities" / "store" / "blob": b"closure",
        project / ".enginery" / "installed" / ".enginery-install.json": b"{}",
        project / "ledger.db": b"ledger",
        project / "artifacts" / "evidence.json": b"evidence",
        Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml": b"schema_version = 1\n",
    }
    written: dict[str, bytes] = {}
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        written[str(path)] = content
    return written


def test_preview_removes_nothing(setup: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _tree(*_roots(setup))

    code, plan = _factory(capsys, setup, "uninstall")

    assert code == 0
    assert plan["mode"] == "preview"
    assert _tree(*_roots(setup)) == before
    actions = {Path(item["path"]).name: item["action"] for item in plan["files"]}  # type: ignore[union-attr]
    assert actions == {"project.toml": "remove", ".enginery": "remove"}
    assert plan["ownership_record"]["action"] == "remove"  # type: ignore[index]


def test_apply_removes_only_unmodified_owned_files_and_the_record_and_never_a_bystander(
    setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bystanders = _bystanders(setup)

    code, plan = _factory(capsys, setup, "uninstall", "--apply")

    assert code == 0
    assert not (setup / ".enginery" / "project.toml").exists()
    assert not _record_path(setup).exists()
    assert (setup / ".enginery").is_dir()  # not empty: it holds the capability store and lock
    kept = {item["path"]: item for item in plan["files"]}  # type: ignore[union-attr]
    assert kept[str(setup / ".enginery")]["action"] == "keep"
    for path, content in bystanders.items():
        assert Path(path).read_bytes() == content


def test_the_policy_directory_setup_created_is_removed_once_empty(
    setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _factory(capsys, setup, "uninstall", "--apply")

    assert code == 0
    assert not (setup / ".enginery").exists()
    assert not _record_path(setup).parent.exists()


def test_a_second_uninstall_is_a_no_op(setup: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _factory(capsys, setup, "uninstall", "--apply")
    before = _tree(*_roots(setup))

    code, plan = _factory(capsys, setup, "uninstall", "--apply")

    assert code == 0
    assert plan["status"] == "nothing-to-uninstall"
    assert plan["removed"] == []
    assert _tree(*_roots(setup)) == before


def test_a_modified_policy_is_kept_as_user_owned_and_the_record_is_still_removed(
    setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = setup / ".enginery" / "project.toml"
    policy.write_text(policy.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    edited = policy.read_bytes()

    code, plan = _factory(capsys, setup, "uninstall", "--apply")

    assert code == 0
    assert policy.read_bytes() == edited
    assert str(policy) in plan["kept_user_owned"]  # type: ignore[operator]
    assert not _record_path(setup).exists()
    code, again = _factory(capsys, setup, "uninstall", "--apply")
    assert again["status"] == "nothing-to-uninstall" and policy.read_bytes() == edited


def test_an_owned_file_that_is_already_gone_is_a_no_op_not_an_error(
    setup: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (setup / ".enginery" / "project.toml").unlink()

    code, plan = _factory(capsys, setup, "uninstall", "--apply")

    assert code == 0
    assert {item["action"] for item in plan["files"]} <= {"already-removed", "remove"}  # type: ignore[union-attr]
    assert not _record_path(setup).exists()


def test_a_tampered_ownership_record_naming_another_path_is_refused(
    setup: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    record = json.loads(_record_path(setup).read_text(encoding="utf-8"))
    record["files"].append(
        {
            "path": str(victim),
            "sha256": hashlib.sha256(b"precious").hexdigest(),
            "role": "project-policy",
        }
    )
    _record_path(setup).write_text(json.dumps(record), encoding="utf-8")

    code, _ = _factory(capsys, setup, "uninstall", "--apply")

    assert code == INVALID
    assert victim.read_text(encoding="utf-8") == "precious"
    assert (setup / ".enginery" / "project.toml").exists()


def test_a_symlink_planted_at_the_policy_path_is_refused(
    setup: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "target.toml"
    target.write_text("keep me", encoding="utf-8")
    policy = setup / ".enginery" / "project.toml"
    policy.unlink()
    policy.symlink_to(target)

    code, _ = _factory(capsys, setup, "uninstall", "--apply")

    assert code == INVALID
    assert target.read_text(encoding="utf-8") == "keep me"
    assert _record_path(setup).exists()


def test_a_project_setup_never_touched_has_nothing_to_uninstall(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = project / ".enginery" / "project.toml"
    policy.parent.mkdir()
    policy.write_text('schema_version = 1\n[runner]\nmode = "trusted-local"\n', encoding="utf-8")

    code, plan = _factory(capsys, project, "uninstall", "--apply")

    assert code == 0
    assert plan["status"] == "nothing-to-uninstall"
    assert policy.exists()
