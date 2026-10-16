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
from enginery.domain.errors import FailureClass, InvalidInputError

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
        [
            "factory",
            "init",
            "--project",
            str(project),
            "--json",
            "--runner",
            "trusted-local",
            *extra,
        ]
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
        if digest:  # a directory carries no content digest
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        else:
            assert Path(path).is_dir()
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


@pytest.mark.parametrize("ancestor", ["enginery", "enginery/projects"])
@pytest.mark.parametrize("apply", [False, True])
def test_private_state_links_refused_without_writes(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ancestor: str,
    apply: bool,
) -> None:
    state = _xdg()[1]
    target = tmp_path / "redirected"
    target.mkdir()
    link = state / ancestor
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    before = _tree(project, target)
    code, _ = _init(capsys, project, *(("--apply",) if apply else ()))
    assert code == INVALID
    assert _tree(project, target) == before


@pytest.mark.parametrize("apply", [False, True])
def test_non_directory_project_state_refused_before_plan(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    apply: bool,
) -> None:
    (project / ".enginery").write_text("user-owned")
    before = _tree(project, *_xdg())
    code, plan = _init(capsys, project, *(("--apply",) if apply else ()))
    assert code == INVALID
    assert not plan
    assert _tree(project, *_xdg()) == before


@pytest.mark.parametrize("capability", ["../bad", "bad/name", "ok@../bad", "ok@x y"])
def test_invalid_capability_refused_without_writes(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    capability: str,
) -> None:
    before = _tree(project, *_xdg())
    assert _init(capsys, project, "--capability", capability, "--apply")[0] == INVALID
    assert _tree(project, *_xdg()) == before


def test_all_created_private_state_directories_are_private(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from enginery.cli.config_locations import private_state_directory

    assert _init(capsys, project, "--apply")[0] == 0
    directory = private_state_directory(project)
    for path in (directory, directory.parent, directory.parent.parent):
        assert path.stat().st_mode & 0o777 == 0o700


def test_failed_record_write_leaves_project_and_state_unchanged(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.cli import factory_setup

    plan = factory_setup.plan_init(
        project, SetupChoices(runner_mode="trusted-local"), capability_versions={}
    )
    before = _tree(project, *_xdg())

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("record write refused")

    monkeypatch.setattr(factory_setup, "write_private_atomically", fail)
    with pytest.raises(OSError, match="record write refused"):
        factory_setup.apply_plan(plan)
    assert _tree(project, *_xdg()) == before


@pytest.mark.parametrize("previous", [False, True])
def test_failed_tracked_write_restores_previous_record_bytes(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous: bool,
) -> None:
    from enginery.cli import factory_setup
    from enginery.cli.config_locations import private_state_directory
    from enginery.cli.setup_files import OWNERSHIP_FILE, OwnershipRecord

    record = private_state_directory(project) / OWNERSHIP_FILE
    if previous:
        record.parent.mkdir(parents=True)
        record.write_bytes(OwnershipRecord(project=str(project), files=()).to_bytes())
    plan = factory_setup.plan_init(
        project, SetupChoices(runner_mode="trusted-local"), capability_versions={}
    )
    before = _tree(project, *_xdg())

    def fail(*args: object, **kwargs: object) -> Path:
        raise OSError("tracked write refused")

    monkeypatch.setattr(factory_setup, "create_exclusive", fail)
    with pytest.raises(OSError, match="tracked write refused"):
        factory_setup.apply_plan(plan)
    assert _tree(project, *_xdg()) == before


def test_failed_record_sync_leaves_project_and_state_unchanged(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.cli import factory_setup, setup_files

    plan = factory_setup.plan_init(
        project, SetupChoices(runner_mode="trusted-local"), capability_versions={}
    )
    before = _tree(project, *_xdg())

    def fail(directory: Path) -> None:
        raise OSError("record sync failed")

    monkeypatch.setattr(setup_files, "fsync_directory", fail)
    with pytest.raises(InvalidInputError, match="record sync failed"):
        factory_setup.apply_plan(plan)
    assert _tree(project, *_xdg()) == before


@pytest.mark.parametrize("ancestor", ["enginery", "enginery/projects"])
def test_apply_rechecks_private_state_links_after_preview(
    project: Path,
    tmp_path: Path,
    ancestor: str,
) -> None:
    from enginery.cli import factory_setup
    from enginery.domain.errors import InvalidInputError

    plan = factory_setup.plan_init(
        project, SetupChoices(runner_mode="trusted-local"), capability_versions={}
    )
    target = tmp_path / "redirected"
    target.mkdir()
    link = _xdg()[1] / ancestor
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(InvalidInputError):
        factory_setup.apply_plan(plan)
    assert not (project / ".enginery").exists()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("apply", [False, True])
def test_old_harness_model_preview_and_apply_are_nonwriting_holds(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply: bool,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    harness = bin_dir / "omp"
    harness.write_text('#!/bin/sh\necho "omp v1.0.0"\n')
    harness.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    before = _tree(project, *_xdg())
    code, plan = _init(
        capsys, project, "--model", "openai/gpt-5.4", *(["--apply"] if apply else [])
    )
    assert code == HOLD
    assert plan["status"] == "hold"
    assert "native controls are unsupported" in str(plan["holds"])
    assert plan["performed"] == []
    holds = plan["holds"]
    assert isinstance(holds, list)
    resolve = holds[0]["resolve"]
    assert "install a omp harness version matching" in resolve
    assert "[executables] in $XDG_CONFIG_HOME/enginery/user.toml" in resolve
    assert (
        main(
            [
                "factory",
                "init",
                "--project",
                str(project),
                "--runner",
                "trusted-local",
                "--model",
                "openai/gpt-5.4",
                *(["--apply"] if apply else []),
            ]
        )
        == HOLD
    )
    text = capsys.readouterr().out
    assert f"resolve: {resolve}" in text
    if apply:
        assert text.rstrip().endswith(
            "apply refused until the harness hold is resolved; nothing was written"
        )
        assert "pass --apply" not in text
    assert _tree(project, *_xdg()) == before


def test_regular_file_state_home_is_a_typed_nonwriting_refusal(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state-file"
    state.write_bytes(b"user-owned")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    before = _tree(project)
    code, _ = _init(capsys, project, "--apply")
    assert code == INVALID
    assert state.read_bytes() == b"user-owned"
    assert _tree(project) == before


def test_read_only_project_apply_is_a_typed_nonwriting_refusal(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _tree(project, *_xdg())
    project.chmod(0o555)
    try:
        code, _ = _init(capsys, project, "--apply")
        assert code == INVALID
        assert _tree(project, *_xdg()) == before
    finally:
        project.chmod(0o755)


@pytest.mark.parametrize("context", [None, "colima", "builder's context"])
def test_init_container_engine_resolution_targets_user_context(
    project: Path, capsys: pytest.CaptureFixture[str], context: str | None
) -> None:
    import shlex
    from typing import cast

    from enginery.cli.main import _build_parser

    if context is not None:
        user = Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(f'schema_version = 1\n[runner]\ndocker_context = "{context}"\n')
    code, plan = _init(
        capsys,
        project,
        "--runner",
        "isolated-oci",
        "--runner-image",
        "sha256:" + "a" * 64,
        "--validation-command",
        "python3 -m unittest",
    )
    assert code == 0
    requirements = cast(list[dict[str, str]], plan["auth_requirements"])
    command = next(item["resolve"] for item in requirements if item["name"] == "container-engine")
    arguments = shlex.split(command)
    assert arguments.pop(0) == "enginery"
    if context is None:
        assert "--docker-context" not in arguments
    else:
        assert f"--docker-context {shlex.quote(context)}" in command
        assert arguments[arguments.index("--docker-context") + 1] == context
    _build_parser().parse_args(arguments)


def test_init_rejects_machine_local_docker_context(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        _init(capsys, project, "--docker-context", "remote")
    assert error.value.code == 2
    assert "unrecognized arguments: --docker-context remote" in capsys.readouterr().err


def test_oversized_existing_policy_is_a_typed_nonwriting_refusal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from enginery.cli.config_locations import MAX_CONFIG_BYTES

    policy = project / ".enginery" / "project.toml"
    policy.parent.mkdir()
    policy.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))
    before = _tree(project, *_xdg())
    assert (
        main(
            [
                "factory",
                "init",
                "--project",
                str(project),
                "--runner",
                "trusted-local",
                "--apply",
            ]
        )
        == INVALID
    )
    assert str(policy) in capsys.readouterr().err
    assert _tree(project, *_xdg()) == before
