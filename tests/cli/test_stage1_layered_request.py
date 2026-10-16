from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.domain.errors import FailureClass
from enginery.workflows.stage1 import stage1_request_from_state
from tests.cli.test_stage1_request_builder import _minimal_argv, _repository

INVALID = exit_code_for(FailureClass.INVALID_INPUT)


def _write_project(repository: Path, text: str) -> Path:
    path = repository / ".enginery" / "project.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"schema_version = 1\n{text}", encoding="utf-8")
    return path


def _write_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> Path:
    config_home = tmp_path / "user-config"
    path = config_home / "enginery" / "user.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return path


def _build(tmp_path: Path, repository: Path, base: str, *extra: str) -> tuple[int, Path]:
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base, output=output
    )
    runner_index = argv.index("--runner")
    del argv[runner_index : runner_index + 2]  # the project or an override supplies the mode
    return main([*argv, *extra]), output


def _start(tmp_path: Path, request: Path) -> int:
    return main(
        [
            "stage1",
            "start",
            "--database",
            str(tmp_path / "ledger.db"),
            "--owner",
            "operator",
            "--request",
            str(request),
        ]
    )


def test_project_user_and_run_layers_reach_the_built_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base = _repository(tmp_path)
    _write_project(
        repository,
        '[runner]\nmode = "trusted-local"\n[validation]\nrepair_limit = 3\n'
        'required_checks = ["lint", "unit"]\n[implementation]\ntime_budget_seconds = 900\n',
    )
    _write_user(
        monkeypatch,
        tmp_path,
        'schema_version = 1\n[validation]\nrepair_limit = 5\nrequired_checks = ["other"]\n'
        '[executables]\nharness = "/opt/omp/bin/omp"\n',
    )

    exit_code, output = _build(
        tmp_path, repository, base, "--implementation-time-budget-seconds", "600"
    )

    assert exit_code == 0
    request = stage1_request_from_state(json.loads(output.read_text(encoding="utf-8")))
    assert request.repair_limit == 3  # project over user
    assert request.required_checks == ("lint", "unit")  # project over user
    assert request.implementation.time_budget_seconds == 600  # run over project
    assert request.execution_configuration.harness_executable == "/opt/omp/bin/omp"  # user
    assert request.runner.isolated_mode is False
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["effective_config"]["fields"]["repair_limit"]["winner"] == "project"
    assert _start(tmp_path, output) == 0


def test_a_missing_user_file_is_not_an_error_and_a_malformed_one_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    _write_project(repository, '[runner]\nmode = "trusted-local"\n')

    assert _build(tmp_path, repository, base)[0] == 0

    path = _write_user(monkeypatch, tmp_path, "schema_version = 1\n[execution]\nplanet = 1\n")
    exit_code, _output = _build(tmp_path, repository, base)
    error = capsys.readouterr().err

    assert exit_code == INVALID
    assert str(path) in error and "execution.planet" in error
    path.write_text("schema_version = 1\n[execution\n", encoding="utf-8")
    assert _build(tmp_path, repository, base)[0] == INVALID


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("[surprise]\nx = 1\n", "surprise"),
        ("[execution]\nharnesses = 1\n", "execution.harnesses"),
        ('[credentials]\ngithub_token = "x"\n', "credential-shaped"),
    ],
)
def test_a_bad_project_file_refuses_the_build_and_writes_no_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, needle: str
) -> None:
    repository, base = _repository(tmp_path)
    path = _write_project(repository, text)

    exit_code, output = _build(tmp_path, repository, base, "--runner", "trusted-local")

    assert exit_code == INVALID
    error = capsys.readouterr().err
    assert str(path) in error and needle in error
    assert not output.exists()


def test_an_unsupported_project_schema_version_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    path = repository / ".enginery" / "project.toml"
    path.parent.mkdir()
    path.write_text("schema_version = 7\n", encoding="utf-8")

    exit_code, output = _build(tmp_path, repository, base, "--runner", "trusted-local")

    assert exit_code == INVALID
    assert "schema_version" in capsys.readouterr().err
    assert not output.exists()


def test_a_symlinked_project_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text("schema_version = 1\n", encoding="utf-8")
    (repository / ".enginery").mkdir()
    (repository / ".enginery" / "project.toml").symlink_to(elsewhere)

    exit_code, output = _build(tmp_path, repository, base, "--runner", "trusted-local")

    assert exit_code == INVALID
    assert "symbolic link" in capsys.readouterr().err
    assert not output.exists()


def test_a_relative_xdg_config_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")

    exit_code, output = _build(tmp_path, repository, base, "--runner", "trusted-local")

    assert exit_code == INVALID
    assert "XDG_CONFIG_HOME" in capsys.readouterr().err
    assert not output.exists()


def test_a_project_hard_constraint_refuses_a_run_override_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    path = _write_project(
        repository, '[runner]\nmode = "trusted-local"\n[constraints]\nmax_repair_limit = 1\n'
    )

    exit_code, output = _build(tmp_path, repository, base, "--repair-limit", "4")

    assert exit_code == INVALID
    error = capsys.readouterr().err
    assert str(path) in error and "constraints.max_repair_limit" in error
    assert not output.exists()


def test_a_layer_naming_another_harness_with_a_pin_surfaces_file_and_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    path = _write_project(
        repository,
        '[runner]\nmode = "trusted-local"\n[execution]\nharness = "claude-code"\n'
        'model = "claude-opus-4-1"\nmodel_pinned = true\n',
    )

    exit_code, output = _build(tmp_path, repository, base, "--harness-provider", "omp")

    assert exit_code == INVALID
    error = capsys.readouterr().err
    assert str(path) in error and "execution.model" in error
    assert not output.exists()


def test_an_unknown_profile_is_refused_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)

    exit_code, output = _build(
        tmp_path, repository, base, "--runner", "trusted-local", "--profile", "reckless"
    )

    assert exit_code == INVALID
    assert "careful" in capsys.readouterr().err
    assert not output.exists()


def test_start_refuses_a_request_edited_after_it_was_built(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    exit_code, output = _build(tmp_path, repository, base, "--runner", "trusted-local")
    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    capsys.readouterr()

    # A field edited without its digest: the request digest no longer matches.
    edited = json.loads(json.dumps(document))
    edited["repair_limit"] = 9
    output.write_text(json.dumps(edited), encoding="utf-8")
    assert _start(tmp_path, output) == INVALID

    # A field edited with its digest recomputed: the bound snapshot disagrees with it.
    request = stage1_request_from_state(document)
    forged = {
        **replace(request, repair_limit=9).initial_state(),
        "effective_config": document["effective_config"],
    }
    output.write_text(json.dumps(forged), encoding="utf-8")
    capsys.readouterr()
    assert _start(tmp_path, output) == INVALID
    assert "repair_limit disagrees with its configuration snapshot" in capsys.readouterr().err

    # A snapshot edited to match: its digest is no longer the one the run binds.
    forged_snapshot = json.loads(json.dumps(document))
    forged_snapshot["effective_config"]["fields"]["repair_limit"]["value"] = 9
    output.write_text(json.dumps(forged_snapshot), encoding="utf-8")
    capsys.readouterr()
    assert _start(tmp_path, output) == INVALID
    assert "configuration_snapshot_digest" in capsys.readouterr().err


def test_start_refuses_a_request_that_predates_layered_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    _, output = _build(tmp_path, repository, base, "--runner", "trusted-local")
    document = json.loads(output.read_text(encoding="utf-8"))
    del document["effective_config"]
    output.write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()

    assert _start(tmp_path, output) == INVALID
    assert "enginery stage1 build-request" in capsys.readouterr().err


def test_a_started_run_resumes_without_its_request_document(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    _, output = _build(tmp_path, repository, base, "--runner", "trusted-local")
    assert _start(tmp_path, output) == 0
    output.unlink()

    assert (
        main(
            [
                "stage1",
                "watch",
                "--database",
                str(tmp_path / "ledger.db"),
                "--owner",
                "operator",
                "--run-id",
                "run-1",
            ]
        )
        == 0
    )
