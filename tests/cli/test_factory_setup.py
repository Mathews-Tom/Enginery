from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from enginery.application.project_policy import SetupChoices, render_project_policy
from enginery.cli._exit_codes import exit_code_for
from enginery.cli.factory_setup import load_project_configuration
from enginery.cli.main import main
from enginery.domain.errors import FailureClass

INVALID = exit_code_for(FailureClass.INVALID_INPUT)
HOLD = exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
CONFLICT = exit_code_for(FailureClass.EXTERNAL_CONFLICT)


def _tree(*roots: Path) -> dict[str, str]:
    """Every file and directory under ``roots`` with a content digest: the full footprint."""
    snapshot: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            key = str(path)
            snapshot[key] = (
                "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            )
    return snapshot


def _xdg() -> tuple[Path, Path]:
    return Path(os.environ["XDG_CONFIG_HOME"]), Path(os.environ["XDG_STATE_HOME"])


def _init(
    capsys: pytest.CaptureFixture[str], project: Path, *extra: str
) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = main(
        ["factory", "init", "--project", str(project), "--json", "--runner", "trusted-local", *extra]
    )
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def test_preview_lists_the_whole_footprint_and_writes_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(project, *_xdg())

    code, plan = _init(capsys, project, "--harness", "omp", "--required-check", "CI")

    assert code == 0
    assert _tree(project, *_xdg()) == before
    assert plan["mode"] == "preview"
    files = {item["role"]: item for item in plan["files"]}  # type: ignore[union-attr]
    assert files["project-policy"]["tracking"] == "tracked"
    assert files["project-policy"]["action"] == "create"
    assert files["project-policy"]["path"] == str(project.resolve() / ".enginery" / "project.toml")
    assert files["ownership-record"]["tracking"] == "private"
    assert files["ownership-record"]["action"] == "create"
    assert plan["network_access"] == []
    assert plan["installs"] == []
    assert {item["name"] for item in plan["auth_requirements"]} == {"github", "harness"}  # type: ignore[union-attr]
    assert plan["performed"] == []


def test_apply_performs_exactly_the_previewed_plan_and_the_policy_compiles(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, preview = _init(capsys, project, "--validation-command", "make test")
    previewed = {item["path"]: item["sha256"] for item in preview["files"]}  # type: ignore[union-attr]

    code, applied = _init(capsys, project, "--validation-command", "make test", "--apply")

    assert code == 0
    assert applied["mode"] == "apply"
    assert set(applied["performed"]) == set(previewed)  # type: ignore[arg-type]
    written = _tree(project, *_xdg())
    for path, digest in previewed.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    assert not [p for p in written if p.endswith(".tmp")]
    configuration = load_project_configuration(project.resolve())
    assert configuration.compiled is not None
    assert configuration.compiled.config.field("runner_mode").winner == "project"
    assert configuration.compiled.config.plain_value("validation_commands") == [["make", "test"]]
    assert str(configuration.compiled.config.digest) == applied["effective_configuration_digest"]


def test_repeating_init_with_the_same_choices_changes_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(capsys, project, "--apply")[0] == 0
    after_first = _tree(project, *_xdg())
    stamps = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}

    code, second = _init(capsys, project, "--apply")

    assert code == 0
    assert second["performed"] == []
    assert {item["action"] for item in second["files"]} == {"unchanged"}  # type: ignore[union-attr]
    assert _tree(project, *_xdg()) == after_first
    assert {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()} == stamps


def test_a_different_existing_project_policy_is_a_user_owned_conflict_never_overwritten(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = project / ".enginery" / "project.toml"
    policy.parent.mkdir()
    policy.write_text('schema_version = 1\n[runner]\nmode = "isolated-oci"\n', encoding="utf-8")
    before = _tree(project, *_xdg())

    code, plan = _init(capsys, project, "--apply")

    assert code == CONFLICT
    assert plan["status"] == "conflict"
    assert plan["performed"] == []
    assert _tree(project, *_xdg()) == before  # not even the ownership record
    assert policy.read_text(encoding="utf-8").count("isolated-oci") == 1


def test_an_identical_user_created_policy_is_left_user_owned(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, preview = _init(capsys, project)
    policy = Path(next(i["path"] for i in preview["files"] if i["role"] == "project-policy"))  # type: ignore[union-attr]
    policy.parent.mkdir()
    policy.write_bytes(render_project_policy(SetupChoices(runner_mode="trusted-local")))

    code, plan = _init(capsys, project, "--apply")

    assert code == 0
    assert plan["performed"] == []
    assert "user-owned" in plan["files"][0]["detail"]  # type: ignore[index]
    assert not (_xdg()[1] / "enginery").exists()


def test_a_symlinked_enginery_directory_is_refused_and_nothing_is_written(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project / ".enginery").symlink_to(elsewhere)

    code, _ = _init(capsys, project, "--apply")

    assert code == INVALID
    assert list(elsewhere.iterdir()) == []
    assert not (_xdg()[1] / "enginery").exists()


def test_a_symlinked_project_policy_is_refused(
    tmp_path: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".enginery").mkdir()
    target = tmp_path / "target.toml"
    target.write_text("schema_version = 1\n", encoding="utf-8")
    (project / ".enginery" / "project.toml").symlink_to(target)

    code, _ = _init(capsys, project, "--apply")

    assert code == INVALID
    assert target.read_text(encoding="utf-8") == "schema_version = 1\n"


def test_a_relative_state_directory_and_a_malformed_user_file_are_refused_before_any_write(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert _init(capsys, project, "--apply")[0] == INVALID
    assert not (project / ".enginery").exists()

    monkeypatch.setenv("XDG_STATE_HOME", str(_xdg()[0].parent / "state"))
    user = _xdg()[0] / "enginery" / "user.toml"
    user.parent.mkdir(parents=True)
    user.write_text("schema_version = 1\n[execution\n", encoding="utf-8")
    assert _init(capsys, project, "--apply")[0] == INVALID
    assert not (project / ".enginery").exists()


def test_an_existing_valid_user_file_is_part_of_the_compiled_configuration_not_touched(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user = _xdg()[0] / "enginery" / "user.toml"
    user.parent.mkdir(parents=True)
    user.write_text("schema_version = 1\n[validation]\nrepair_limit = 3\n", encoding="utf-8")
    before = user.read_bytes()

    code, plan = _init(capsys, project, "--apply")

    assert code == 0
    assert user.read_bytes() == before
    configuration = load_project_configuration(project.resolve())
    assert configuration.compiled is not None
    assert configuration.compiled.config.value("repair_limit") == 3
    assert configuration.compiled.config.field("repair_limit").winner == "user"
    assert str(configuration.compiled.config.digest) == plan["effective_configuration_digest"]


def test_a_selected_capability_that_is_not_installed_is_a_hold_naming_the_exact_commands(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, plan = _init(capsys, project, "--capability", "review-skill@1.2.0", "--apply")

    assert code == HOLD
    assert plan["status"] == "hold"
    (install,) = plan["installs"]  # type: ignore[misc]
    assert install["performed_by_init"] is False
    assert install["commands"][0].startswith("enginery capability preview review-skill@1.2.0 ")
    assert install["commands"][1].startswith("enginery capability install review-skill@1.2.0 ")
    # init still wrote the policy it was asked to, and nothing under the install root
    configuration = load_project_configuration(project.resolve())
    assert configuration.compiled is not None
    assert configuration.compiled.config.plain_value("permitted_capabilities") == [
        "git",
        "review-skill",
    ]
    assert not (project / ".enginery" / "installed").exists()


def test_an_invalid_choice_is_refused_with_no_writes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _init(capsys, project, "--profile", "reckless", "--apply")
    assert code == INVALID
    code, _ = _init(capsys, project, "--runner-image", "sha256:" + "a" * 64, "--apply")
    assert code == INVALID  # an image applies only to isolated-oci
    assert not (project / ".enginery").exists()
