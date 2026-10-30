from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginery.cli import factory_run
from enginery.cli.main import main
from tests.cli.test_factory_run import bound_project  # noqa: F401
from tests.cli.test_request_compiler import _git, _minimal_argv, _repository
from tests.cli.test_runner_cli import _admission_factory, _runner_image_fake
from tests.support.behavioral import declare_behavioral_policy
from tests.support.github import StandInGitHub

IMAGE = "sha256:" + "a" * 64
ISOLATED = [
    "--runner",
    "isolated-oci",
    "--runner-image",
    IMAGE,
    "--validation-command",
    "python3 -m unittest",
]


@pytest.fixture(autouse=True)
def _temporary_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _own_project(project: Path, text: str = '[project]\nname = "enginery"\n') -> None:
    (project / "pyproject.toml").write_text(text, encoding="utf-8")


def _assert_refusal(code: int, capsys: pytest.CaptureFixture[str]) -> None:
    error = capsys.readouterr().err
    assert code == 2
    assert "--runner isolated-oci" in error
    assert "trusted-local" in error
    assert "installed runtime, config, ledger and credentials" in error


def test_init_own_repository_requires_isolation_and_can_apply_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _own_project(tmp_path)
    args = ["init", "--project", str(tmp_path), "--apply"]
    _assert_refusal(main([*args, "--runner", "trusted-local"]), capsys)
    assert not (tmp_path / ".enginery").exists()
    assert main([*args, *ISOLATED]) == 0
    assert 'mode = "isolated-oci"' in (tmp_path / ".enginery/project.toml").read_text()


def test_build_request_own_repository_requires_isolation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, base = _repository(tmp_path)
    _own_project(project)
    output = tmp_path / "request.json"
    args = _minimal_argv(tmp_path=tmp_path, repository=project, base_revision=base, output=output)
    _assert_refusal(main(args), capsys)
    assert not output.exists()
    assert main([*args, *ISOLATED]) == 0
    assert json.loads(output.read_text())["runner"]["trust_mode"] == "isolated-oci"


def test_run_own_repository_requires_isolation_before_worker_execution(
    bound_project: tuple[Path, StandInGitHub],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    _own_project(project)
    args = ["run", "owner/repo#1", "--project", str(project), "--once", "--json"]
    _assert_refusal(main(args), capsys)
    assert not factory_run.read_index(project).repositories
    policy = project / ".enginery/project.toml"
    policy.write_text(
        policy.read_text().replace(
            'mode = "trusted-local"', f'mode = "isolated-oci"\nimage = "{IMAGE}"'
        )
        + '\n[validation]\ncommands = ["python3 -m unittest"]\n'
    )
    _git("add", ".enginery/project.toml", cwd=project)
    _git("commit", "-m", "select isolated worker", cwd=project)
    _assert_refusal(main([*args, "--runner", "trusted-local"]), capsys)
    # Admission and persisted request are real; container execution is outside this CLI test.
    monkeypatch.setattr(
        "enginery.cli.runner_wiring.OciRunnerAdmission", _admission_factory(_runner_image_fake())
    )
    monkeypatch.setattr(factory_run, "advance_run", lambda *a, **kw: {"status": "ok"})
    assert main([*args, "--validation-command", "python3 -m unittest"]) == 0
    intake = factory_run.read_index(project)
    entry = next(iter(next(iter(intake.repositories.values())).issues.values()))
    request = json.loads(factory_run.request_path(project, entry.run_id).read_text())
    assert request["runner"]["trust_mode"] == "isolated-oci"


def test_other_projects_keep_trusted_local(tmp_path: Path) -> None:
    _own_project(tmp_path, '[project]\nname = "another-project"\n')
    assert main(["init", "--project", str(tmp_path), "--runner", "trusted-local", "--apply"]) == 0
    assert 'mode = "trusted-local"' in (tmp_path / ".enginery/project.toml").read_text()


@pytest.mark.parametrize("content", ["[project\n", "x" * (1024 * 1024 + 1)])
def test_existing_bad_project_metadata_is_not_silently_ignored(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _own_project(tmp_path, content)
    args = ["init", "--project", str(tmp_path)]
    assert main([*args, "--runner", "trusted-local"]) == 2
    error = capsys.readouterr().err
    assert "cannot determine whether this is Enginery's own repository" in error
    assert "fix pyproject.toml or use --runner isolated-oci" in error
    assert not (tmp_path / ".enginery").exists()
    assert main([*args, *ISOLATED]) == 0


def test_advancing_a_previously_bound_trusted_run_refuses_own_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, base = _repository(tmp_path)
    base = declare_behavioral_policy(project)
    output = tmp_path / "request.json"
    assert (
        main(
            _minimal_argv(tmp_path=tmp_path, repository=project, base_revision=base, output=output)
        )
        == 0
    )
    database = tmp_path / "ledger.sqlite"
    common = ["--database", str(database), "--owner", "operator"]
    assert main(["engine", "start", *common, "--request", str(output)]) == 0
    _own_project(project)
    capsys.readouterr()
    _assert_refusal(main(["engine", "next", *common, "--run-id", "run-1", "--advance"]), capsys)
