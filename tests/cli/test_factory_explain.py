from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.domain.errors import FailureClass
from tests.cli.test_stage1_request_builder import _minimal_argv, _repository

HOLD = exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)


def _tree(*roots: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for root in roots:
        if root.exists():
            for path in sorted(root.rglob("*")):
                snapshot[str(path)] = (
                    "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
                )
    return snapshot


def _explain(capsys: pytest.CaptureFixture[str], project: Path) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = main(["explain", "--project", str(project), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _field(document: dict[str, object], name: str) -> dict[str, object]:
    return next(row for row in document["fields"] if row["name"] == name)  # type: ignore[union-attr]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_every_field_names_its_winning_layer_shadowed_values_and_source_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    policy = project / ".enginery" / "project.toml"
    _write(
        policy,
        'schema_version = 1\nprofile = "careful"\n[runner]\nmode = "trusted-local"\n'
        "[validation]\nrepair_limit = 4\n[constraints]\nmax_repair_limit = 5\n",
    )
    user = Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
    _write(user, "schema_version = 1\n[validation]\nrepair_limit = 3\n")

    code, document = _explain(capsys, project)

    assert code == 0
    repair = _field(document, "repair_limit")
    assert repair["value"] == 4 and repair["winner"] == "project"
    chain = [(entry["layer"], entry["value"]) for entry in repair["chain"]]  # type: ignore[union-attr]
    assert chain == [("project", 4), ("user", 3), ("workflow", 2), ("factory", 1)]
    assert [entry["layer"] for entry in repair["shadowed"]] == ["user", "workflow", "factory"]  # type: ignore[union-attr]
    project_entry = repair["chain"][0]  # type: ignore[index]
    assert project_entry["source"] == str(policy.resolve())
    assert (
        project_entry["source_digest"]
        == "sha256:" + hashlib.sha256(policy.read_bytes()).hexdigest()
    )
    assert {c["name"] for c in document["hard_constraints"]} == {"max_repair_limit"}  # type: ignore[union-attr]
    assert _field(document, "profile")["value"] == "careful"
    assert {s["layer"] for s in document["sources"]} == {
        "project",
        "user",
        "workflow",
        "factory",
        "run",
    }  # type: ignore[union-attr]


def test_explain_shows_the_same_digest_build_request_binds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from enginery.application.effective_config import EffectiveConfig
    from enginery.workflows.stage1 import stage1_request_from_state
    from tests.support.github import write_stand_in_gh

    repository, base = _repository(tmp_path)
    github = write_stand_in_gh(tmp_path / "github")
    _write(
        repository / ".enginery" / "project.toml",
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
        '[execution]\nmodel = "openai/gpt-5"\neffort = "high"\n',
    )
    _write(
        Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml",
        f'schema_version = 1\n[executables]\ngithub = "{github.executable}"\n',
    )
    _, document = _explain(capsys, repository)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base, output=output, github=github
    )
    for flag in ("--runner", "--github-executable"):
        del argv[argv.index(flag) : argv.index(flag) + 2]

    assert main(argv) == 0

    built = json.loads(output.read_text(encoding="utf-8"))
    assert document["effective_configuration_digest"] == str(
        EffectiveConfig.from_state(built["effective_config"]).digest
    )
    request = stage1_request_from_state(built)
    assert document["effective_configuration_digest"] == str(
        request.run.configuration_snapshot_digest
    )


def test_setup_owned_files_are_reported_owned_modified_or_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert main(["init", "--project", str(project), "--runner", "trusted-local", "--apply"]) == 0
    policy = project.resolve() / ".enginery" / "project.toml"

    _, document = _explain(capsys, project)
    statuses = {Path(item["path"]).name: item["status"] for item in document["setup_owned_files"]}  # type: ignore[union-attr]
    assert statuses == {".enginery": "owned", "project.toml": "owned"}

    policy.write_text(policy.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    _, document = _explain(capsys, project)
    assert {Path(i["path"]).name: i["status"] for i in document["setup_owned_files"]}[
        "project.toml"
    ] == "user-modified"  # type: ignore[union-attr]

    policy.unlink()
    _, document = _explain(capsys, project)
    assert {Path(i["path"]).name: i["status"] for i in document["setup_owned_files"]}[
        "project.toml"
    ] == "missing"  # type: ignore[union-attr]


def test_a_policy_setup_did_not_create_is_reported_user_owned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    _write(
        project / ".enginery" / "project.toml",
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n',
    )

    _, document = _explain(capsys, project)

    (item,) = document["setup_owned_files"]  # type: ignore[misc]
    assert item["status"] == "user-owned"


def test_current_holds_are_listed_and_explain_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with_capability = tmp_path / "with-capability"
    _write(
        with_capability / ".enginery" / "project.toml",
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
        '[implementation]\npermitted_capabilities = ["git", "review-skill"]\n',
    )
    before = _tree(tmp_path, Path(os.environ["XDG_STATE_HOME"]))

    code, document = _explain(capsys, empty)
    assert code == HOLD
    assert document["fields"] == []
    assert document["holds"][0]["prerequisite"] == "trust mode"  # type: ignore[index]
    assert document["holds"][0]["resolve"].startswith("enginery init")  # type: ignore[index]

    code, document = _explain(capsys, with_capability)
    assert code == HOLD
    assert [hold["prerequisite"] for hold in document["holds"]] == ["capability review-skill"]  # type: ignore[union-attr]
    assert _tree(tmp_path, Path(os.environ["XDG_STATE_HOME"])) == before


def test_a_malformed_policy_is_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    _write(project / ".enginery" / "project.toml", "schema_version = 9\n")

    assert main(["explain", "--project", str(project)]) == exit_code_for(FailureClass.INVALID_INPUT)
    assert "schema_version" in capsys.readouterr().err


@pytest.mark.parametrize("entry", ["absolute", "traversal", "outside-directory", "symlink"])
def test_invalid_ownership_entries_are_never_reported_owned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    from enginery.cli.config_locations import private_state_directory
    from enginery.cli.setup_files import OWNERSHIP_FILE

    project = tmp_path / "project"
    project.mkdir()
    assert main(["init", "--project", str(project), "--runner", "trusted-local", "--apply"]) == 0
    record_path = private_state_directory(project.resolve()) / OWNERSHIP_FILE
    record = json.loads(record_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside"
    outside.mkdir()
    if entry == "absolute":
        record["files"][0]["path"] = str(outside / "project.toml")
    elif entry == "traversal":
        record["files"][0]["path"] = str(project / ".." / "outside")
    elif entry == "outside-directory":
        record["files"][0].update(path=str(outside), role="directory")
    else:
        policy = project / ".enginery" / "project.toml"
        policy.unlink()
        policy.symlink_to(outside / "project.toml")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    capsys.readouterr()

    assert main(["explain", "--project", str(project), "--json"]) == exit_code_for(
        FailureClass.INVALID_INPUT
    )
    captured = capsys.readouterr()
    assert '"owned"' not in captured.out


def test_existing_policy_trust_hold_names_an_actionable_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    policy = project / ".enginery" / "project.toml"
    _write(policy, "schema_version = 1\n")

    code, document = _explain(capsys, project)

    assert code == HOLD
    resolution = document["holds"][0]["resolve"]  # type: ignore[index]
    assert str(policy) in resolution
    assert "trusted-local" in resolution and "isolated-oci" in resolution
    assert "enginery init" not in resolution


@pytest.mark.parametrize("locked", [False, True])
def test_capability_hold_previews_then_installs_the_locked_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], locked: bool
) -> None:
    from enginery.capabilities.serialization import write_lock
    from tests.support.capabilities import lock_of, locked_entry

    project = tmp_path / "project"
    _write(
        project / ".enginery" / "project.toml",
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
        '[implementation]\npermitted_capabilities = ["review-skill"]\n',
    )
    if locked:
        write_lock(
            lock_of(locked_entry("review-skill", version="2.3")),
            project / ".enginery" / "capabilities.lock.json",
        )

    code, document = _explain(capsys, project)

    assert code == HOLD
    resolution = document["holds"][0]["resolve"]  # type: ignore[index]
    request = "review-skill@" + ("2.3" if locked else "<version>")
    assert f"enginery capability preview {request} " in resolution
    assert f" && enginery capability install {request} " in resolution


@pytest.mark.parametrize("layer", ["project", "user"])
def test_invalid_capability_name_is_invalid_input_naming_its_file_and_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], layer: str
) -> None:
    project = tmp_path / "project"
    policy = project / ".enginery" / "project.toml"
    _write(policy, 'schema_version = 1\n[runner]\nmode = "trusted-local"\n')
    source = (
        policy
        if layer == "project"
        else Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
    )
    prefix = policy.read_text(encoding="utf-8") if layer == "project" else "schema_version = 1\n"
    _write(source, prefix + '[implementation]\npermitted_capabilities = ["bad;command"]\n')
    capsys.readouterr()

    code = main(["explain", "--project", str(project), "--json"])

    assert code == exit_code_for(FailureClass.INVALID_INPUT)
    error = capsys.readouterr().err
    assert str(source) in error and "implementation.permitted_capabilities" in error


def test_incompatible_harness_model_default_is_an_explain_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    executable = tmp_path / "omp"
    _write(executable, "#!/bin/sh\nprintf 'omp v99.0.0\\n'\n")
    executable.chmod(0o755)
    _write(
        project / ".enginery" / "project.toml",
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
        '[execution]\nmodel = "openai/gpt-5"\n',
    )
    _write(
        Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml",
        f'schema_version = 1\n[executables]\nharness = "{executable}"\n',
    )

    code, document = _explain(capsys, project)

    assert code == HOLD
    holds = document["holds"]
    assert isinstance(holds, list)
    hold = next(item for item in holds if item["prerequisite"] == "harness-omp")
    assert "99.0.0" in hold["detail"]
    assert "install" in hold["resolve"] and "omp" in hold["resolve"]
