from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from enginery.application.factory_intake import derive_run_id
from enginery.cli import factory_run
from enginery.cli.config_locations import private_state_directory, project_identity
from enginery.cli.main import main
from enginery.workflows.stage1 import stage1_request_from_state
from tests.cli.test_stage1_request_builder import _git, _repository
from tests.support.github import StandInGitHub, write_stand_in_gh


@pytest.fixture
def bound_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, StandInGitHub]:
    project, _ = _repository(tmp_path)
    _git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=project)
    github = write_stand_in_gh(tmp_path / "github")
    harness = tmp_path / "omp"
    harness.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "if '--help' in sys.argv:\n print('omp v18.10.3')\n"
        "else:\n"
        " for event in ('session', 'agent_start', 'agent_end'):\n"
        "  print(json.dumps({'type': event}))\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    (project / ".enginery").mkdir()
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n', encoding="utf-8"
    )
    user = Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(
        f'schema_version = 1\n[executables]\ngithub = "{github.executable}"\n'
        f'harness = "{harness}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(factory_run, "intake_prerequisites", lambda _: [])
    return project, github


def invoke(capsys: pytest.CaptureFixture[str], project: Path, *arguments: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = main(["factory", *arguments, "--project", str(project), "--json"])
    output = capsys.readouterr()
    assert output.out, output.err
    return code, json.loads(output.out)


def test_run_binds_compiler_request_and_status_explains_embedded_selection(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    code, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0
    raw = json.loads(Path(run["request_path"]).read_text())
    request = stage1_request_from_state(raw)
    assert run["run_id"] == derive_run_id(
        project_identity(project), "owner/repo#1", str(request.work_snapshot.bound_digest)
    )
    assert (
        request.workspace_path
        == private_state_directory(project) / "runs" / run["run_id"] / "workspace"
    )
    assert request.implementation.permitted_capabilities == tuple(run["permitted_capabilities"])
    code, status = invoke(capsys, project, "status", run["run_id"])
    assert code == 0 and status["request_digest"] == str(request.digest)
    assert status["budget"]["cost"] == "unenforced"
    assert status["selection"] == run["selection"]
    code, explanation = invoke(capsys, project, "explain", run["run_id"])
    assert code == 0 and explanation["fields"] == run["fields"]
    assert (
        next(field for field in explanation["fields"] if field["name"] == "runner_mode")["winner"]
        == "project"
    )
    _, listing = invoke(capsys, project, "status")
    assert [item["run_id"] for item in listing["runs"]] == [run["run_id"]]


def test_changed_issue_holds_without_replacing_request(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, github = bound_project
    _, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    path = Path(run["request_path"])
    before = path.read_bytes()
    github.serve(updated_at="2026-10-17T00:00:00Z")
    code, held = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 5
    assert held["holds"][0]["detail"] == f"issue changed since run {run['run_id']} was bound"
    assert "--supersede" in held["holds"][0]["resolve"]
    assert path.read_bytes() == before


def test_repeat_run_reuses_durable_request_and_workspace(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    _, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    path = Path(run["request_path"])
    before = path.read_bytes()
    code, repeated = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0 and repeated["run_id"] == run["run_id"]
    assert path.read_bytes() == before
    assert len(list(path.parent.parent.glob("*/request.json"))) == 1
    assert (
        main(
            [
                "stage1",
                "cancel",
                "--database",
                run["database"],
                "--owner",
                "factory",
                "--run-id",
                run["run_id"],
                "--node-id",
                "implement",
            ]
        )
        == 0
    )
