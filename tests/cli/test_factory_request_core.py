from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.cli.stage1_request import (
    RequestInputs,
    add_build_request_parser,
    build_request,
    compile_request,
)
from enginery.domain.errors import InvalidInputError
from tests.cli.test_stage1_layered_request import _write_project, _write_user
from tests.cli.test_stage1_request_builder import _minimal_argv, _repository
from tests.support.github import write_stand_in_gh


@pytest.mark.parametrize("overrides", [False, True])
def test_factory_inputs_compile_the_same_bound_document_as_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: bool
) -> None:
    repository, base = _repository(tmp_path)
    _write_project(
        repository,
        '[runner]\nmode = "trusted-local"\n[validation]\nrepair_limit = 2\n'
        'required_checks = ["unit"]\n[implementation]\ntime_budget_seconds = 900\n',
    )
    _write_user(monkeypatch, tmp_path, "schema_version = 1\n")
    github = write_stand_in_gh(tmp_path / "github")
    output = tmp_path / "request.json"
    inputs = RequestInputs(
        output=output,
        run_id="run-1",
        repository="owner/repo",
        external_reference="owner/repo#1",
        base_revision=base,
        repository_path=repository,
        workspace_path=tmp_path / "workspace",
        artifact_root=tmp_path / "artifacts",
        github_executable=str(github.executable),
        runner="trusted-local",
    )
    argv = _minimal_argv(
        tmp_path=tmp_path,
        repository=repository,
        base_revision=base,
        output=output,
        github=github,
    )[1:]
    if overrides:
        inputs = replace(
            inputs,
            base_branch="release",
            head_branch="factory/custom",
            validation_commands=["git diff --check"],
            required_checks=["lint", "integration"],
            repair_limit=0,
            protected_paths=["secrets/**"],
            implementation_time_budget_seconds=600,
            inapplicable_criteria=[0],
            environment_manifest_digest="sha256:" + "a" * 64,
        )
        argv.extend(
            [
                "--base-branch",
                "release",
                "--head-branch",
                "factory/custom",
                "--validation-command",
                "git diff --check",
                "--required-check",
                "lint",
                "--required-check",
                "integration",
                "--repair-limit",
                "0",
                "--protected-path",
                "secrets/**",
                "--implementation-time-budget-seconds",
                "600",
                "--inapplicable-criterion",
                "0",
                "--environment-manifest-digest",
                "sha256:" + "a" * 64,
            ]
        )
    parser = argparse.ArgumentParser()
    add_build_request_parser(parser.add_subparsers())

    cli_built = build_request(parser.parse_args(argv))
    factory_built = compile_request(inputs)

    assert factory_built.document() == cli_built.document()
    assert factory_built.request.digest == cli_built.request.digest
    assert not output.exists()
    assert not inputs.workspace_path.exists()
    assert not inputs.artifact_root.exists()


def test_compiler_refuses_unenforceable_cost_before_reading_the_source(tmp_path: Path) -> None:
    inputs = RequestInputs(
        output=tmp_path / "request.json",
        run_id="run-1",
        repository="owner/repo",
        external_reference="owner/repo#1",
        base_revision="a" * 40,
        repository_path=tmp_path,
        workspace_path=tmp_path / "workspace",
        artifact_root=tmp_path / "artifacts",
        implementation_cost_budget="1.00",
    )

    with pytest.raises(InvalidInputError, match="no supported harness enforces"):
        compile_request(inputs)

    assert not inputs.output.exists()
