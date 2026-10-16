from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from enginery.cli import factory_diagnostics
from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.domain.errors import FailureClass
from enginery.domain.execution_selection import HoldReason

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


def _script(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def toolbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH holding only recording stand-ins for git, gh and omp."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    _script(bin_dir, "git", f'echo "git $*" >> {calls}; echo "git version 2.99.0"')
    _script(
        bin_dir,
        "gh",
        f'echo "gh $*" >> {calls}; '
        'if [ "$1 $2" = "auth status" ]; then exit "${GH_AUTH_EXIT:-0}"; fi',
    )
    _script(bin_dir, "omp", f'echo "omp $*" >> {calls}; echo "omp v18.10.3"')
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / ".enginery").mkdir(parents=True)
    (path / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n', encoding="utf-8"
    )
    return path


def _doctor(capsys: pytest.CaptureFixture[str], project: Path) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = main(["factory", "doctor", "--project", str(project), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _statuses(report: dict[str, object]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in report["checks"]}  # type: ignore[index,union-attr]


def test_a_satisfied_project_reports_ok_and_mutates_nothing(
    toolbox: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(project, Path(os.environ["XDG_CONFIG_HOME"]), Path(os.environ["XDG_STATE_HOME"]))

    code, report = _doctor(capsys, project)

    assert code == 0
    assert report["status"] == "ok"
    assert _statuses(report) == {
        "git": "ok",
        "github-cli": "ok",
        "github-auth": "ok",
        "harness-omp": "ok",
    }
    assert report["mutations"] == []
    assert (
        _tree(project, Path(os.environ["XDG_CONFIG_HOME"]), Path(os.environ["XDG_STATE_HOME"]))
        == before
    )
    calls = (toolbox.parent / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "gh auth status" in calls and "gh auth login" not in calls


def test_missing_github_auth_is_a_hold_naming_the_resolving_command(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_AUTH_EXIT", "1")

    code, report = _doctor(capsys, project)

    assert code == HOLD
    assert report["status"] == "hold"
    (hold,) = report["holds"]  # type: ignore[misc]
    assert hold["name"] == "github-auth"
    assert hold["resolve"] == "gh auth login"


@pytest.mark.parametrize(
    ("missing", "name", "resolve_text"),
    [
        ("git", "git", "install git"),
        ("gh", "github-cli", "install the GitHub CLI"),
        ("omp", "harness-omp", "install the omp CLI"),
    ],
)
def test_a_missing_executable_is_a_hold_naming_the_prerequisite_and_the_fix(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    missing: str,
    name: str,
    resolve_text: str,
) -> None:
    (toolbox / missing).unlink()

    code, report = _doctor(capsys, project)

    assert code == HOLD
    held = {hold["name"]: hold for hold in report["holds"]}  # type: ignore[union-attr]
    assert name in held
    assert resolve_text in held[name]["resolve"]
    assert report["status"] == "hold"


def test_an_unaccepted_harness_version_is_a_hold_not_a_ready_claim(
    toolbox: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _script(toolbox, "omp", 'echo "omp v1.0.0"')

    code, report = _doctor(capsys, project)

    assert code == HOLD
    assert _statuses(report)["harness-omp"] == "hold"


def test_only_what_the_selection_needs_is_checked(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        factory_diagnostics,
        "OciRunnerAdmission",
        lambda **_: pytest.fail("a trusted-local project must not consult a container engine"),
    )
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[execution]\nharness = "claude-code"\n'
        '[runner]\nmode = "trusted-local"\n',
        encoding="utf-8",
    )

    _, report = _doctor(capsys, project)

    names = set(_statuses(report))
    assert "harness-claude-code" in names and "harness-omp" not in names
    assert not any(name.startswith("container") for name in names)


def test_an_isolated_project_checks_the_engine_and_not_the_host_harness(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Hold:
        message = "no qualified container engine is reachable"
        reason = HoldReason.RUNNER_UNQUALIFIED

    class _Decision:
        hold = _Hold()

    class _Admission:
        def __init__(self, **_: object) -> None: ...

        def admit(self, selection: object) -> _Decision:
            return _Decision()

    monkeypatch.setattr(factory_diagnostics, "OciRunnerAdmission", _Admission)
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "isolated-oci"\n'
        f'image = "sha256:{"a" * 64}"\n[validation]\ncommands = ["python3 -m unittest"]\n',
        encoding="utf-8",
    )
    (toolbox / "omp").unlink()

    code, report = _doctor(capsys, project)

    assert code == HOLD
    statuses = _statuses(report)
    assert statuses["container-runner"] == "hold"
    assert statuses["harness-omp"] == "not-needed"
    (hold,) = report["holds"]  # type: ignore[misc]
    assert "container engine" in hold["resolve"]
    assert "docker_context" in hold["resolve"]


def test_a_permitted_capability_that_is_not_installed_is_a_hold_with_the_install_commands(
    toolbox: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
        '[implementation]\npermitted_capabilities = ["git", "review-skill"]\n',
        encoding="utf-8",
    )

    code, report = _doctor(capsys, project)

    assert code == HOLD
    held = {hold["name"]: hold for hold in report["holds"]}  # type: ignore[union-attr]
    assert (
        "enginery capability install review-skill@<version>"
        in held["capability-review-skill"]["resolve"]
    )


def test_a_project_without_a_declared_trust_mode_is_held_with_the_init_command(
    toolbox: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    code, report = _doctor(capsys, empty)

    assert code == HOLD
    (hold,) = report["holds"]  # type: ignore[misc]
    assert hold["name"] == "trust-mode"
    assert hold["resolve"].startswith("enginery factory init --runner")


def test_a_malformed_project_policy_is_invalid_input_not_a_hold(
    toolbox: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".enginery" / "project.toml").write_text(
        "schema_version = 1\nsurprise = true\n", encoding="utf-8"
    )

    assert main(["factory", "doctor", "--project", str(project)]) == exit_code_for(
        FailureClass.INVALID_INPUT
    )
    assert "surprise" in capsys.readouterr().err


def test_failed_git_version_is_a_repair_hold(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _script(toolbox, "git", "echo broken >&2; exit 1")
    code, report = _doctor(capsys, project)
    assert code == HOLD
    holds = report["holds"]
    assert any(
        item["name"] == "git" and item["resolve"] == "repair the git install"
        for item in holds  # type: ignore[union-attr]
    )


def test_existing_policy_trust_hold_names_edit_and_edit_clears_it(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = project / ".enginery" / "project.toml"
    policy.write_text("schema_version = 1\n")
    code, report = _doctor(capsys, project)
    assert code == HOLD
    (hold,) = report["holds"]  # type: ignore[misc]
    assert str(policy) in hold["resolve"]
    assert "[runner]" in hold["resolve"]
    policy.write_text('schema_version = 1\n[runner]\nmode = "trusted-local"\n')
    assert _doctor(capsys, project)[0] == 0


def test_engine_unreachable_hold_names_engine_not_image_repair(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _script(toolbox, "docker", "echo 'engine unreachable' >&2; exit 1")
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "isolated-oci"\n'
        f'image = "sha256:{"a" * 64}"\n[validation]\ncommands = ["python3 -m unittest"]\n'
    )
    code, report = _doctor(capsys, project)
    assert code == HOLD
    holds = report["holds"]
    runner = next(
        item
        for item in holds
        if item["name"] == "container-runner"  # type: ignore[union-attr]
    )
    assert "container engine" in runner["resolve"]
    assert "docker_context" in runner["resolve"]
    assert "build-image" not in runner["resolve"]


def test_doctor_digest_matches_build_request_without_run_overrides(
    toolbox: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.workflows.stage1 import stage1_request_from_state
    from tests.cli.test_stage1_request_builder import _minimal_argv, _repository

    with monkeypatch.context() as context:
        context.setenv("PATH", os.defpath)
        repository, base = _repository(tmp_path)
    policy = repository / ".enginery" / "project.toml"
    policy.parent.mkdir()
    policy.write_text(
        'schema_version = 1\n[execution]\nmodel = "openai/gpt-5.4"\neffort = "high"\n'
        '[runner]\nmode = "trusted-local"\n'
    )
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base, output=output
    )
    github_index = argv.index("--github-executable")
    github = Path(argv[github_index + 1])
    (toolbox / "gh").write_bytes(github.read_bytes())
    (toolbox / "gh").chmod(0o755)
    (toolbox / "gh-state.json").write_bytes((github.parent / "gh-state.json").read_bytes())
    for flag in ("--runner", "--github-executable"):
        index = argv.index(flag)
        del argv[index : index + 2]
    assert main(argv) == 0
    request = stage1_request_from_state(json.loads(output.read_text()))
    _, report = _doctor(capsys, repository)
    assert report["effective_configuration_digest"] == str(
        request.run.configuration_snapshot_digest
    )


@pytest.mark.parametrize(
    ("binary", "key", "check"),
    [
        ("gh", "github", "github-cli"),
        ("omp", "harness", "harness-omp"),
    ],
)
def test_executable_hold_resolves_through_user_preferences(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    binary: str,
    key: str,
    check: str,
) -> None:
    original = toolbox / binary
    alternate = toolbox / f"alternate-{binary}"
    original.rename(alternate)
    code, report = _doctor(capsys, project)
    assert code == HOLD
    holds = cast(list[dict[str, str]], report["holds"])
    hold = next(item for item in holds if item["name"] == check)
    assert "$XDG_CONFIG_HOME/enginery/user.toml" in hold["resolve"]
    assert ".enginery/project.toml" not in hold["resolve"]
    user = Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(f'schema_version = 1\n[executables]\n{key} = "{alternate}"\n')
    code, report = _doctor(capsys, project)
    assert _statuses(report)[check] == "ok"
    assert code == 0


def test_model_default_with_old_harness_is_reported_as_hold(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _script(toolbox, "omp", 'echo "omp v1.0.0"')
    policy = project / ".enginery" / "project.toml"
    policy.write_text(policy.read_text() + '\n[execution]\nmodel = "openai/gpt-5"\n')
    code, report = _doctor(capsys, project)
    assert code == HOLD
    assert _statuses(report)["harness-omp"] == "hold"
    assert _statuses(report)["git"] == "ok"
    assert _statuses(report)["github-auth"] == "ok"
    assert "version" in str(report["holds"])


def test_invalid_capability_name_is_not_rendered_as_command(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = project / ".enginery" / "project.toml"
    policy.write_text(
        policy.read_text() + '\n[implementation]\npermitted_capabilities = ["$(id)"]\n'
    )
    assert main(["factory", "doctor", "--project", str(project), "--json"]) == 2
    output = capsys.readouterr()
    assert "implementation.permitted_capabilities" in output.err
    assert "enginery capability preview" not in output.out


def test_existing_runner_table_resolution_does_not_add_duplicate_table(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = project / ".enginery" / "project.toml"
    policy.write_text("schema_version = 1\n[runner]\n")
    code, report = _doctor(capsys, project)
    assert code == HOLD
    assert "add the table if it is absent" in str(report["holds"])
    policy.write_text(policy.read_text() + 'mode = "trusted-local"\n')
    assert _doctor(capsys, project)[0] == 0


def test_relay_resolution_flags_exist_on_real_parser(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shlex

    from enginery.cli.main import _build_parser

    class _Hold:
        message = "model relay is not running"
        reason = HoldReason.RUNNER_UNQUALIFIED

    class _Decision:
        hold = _Hold()

    class _Admission:
        def __init__(self, **_: object) -> None: ...

        def admit(self, selection: object) -> _Decision:
            return _Decision()

    monkeypatch.setattr(factory_diagnostics, "OciRunnerAdmission", _Admission)
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "isolated-oci"\n'
        f'image = "sha256:{"a" * 64}"\n[validation]\ncommands = ["python3 -m unittest"]\n'
    )
    code, report = _doctor(capsys, project)
    assert code == HOLD
    holds = cast(list[dict[str, str]], report["holds"])
    resolve = next(item["resolve"] for item in holds if item["name"] == "container-runner")
    assert "relay" in resolve
    filled = (
        resolve.replace("<relay image id>", "b" * 64)
        .replace("<gateway URL>", "https://gateway.example")
        .replace("<absolute token file>", str(project / "token"))
    )
    commands = filled.split(", then ")
    assert len(commands) == 3
    for command in commands:
        arguments = shlex.split(command)
        assert arguments.pop(0) == "enginery"
        _build_parser().parse_args(arguments)


@pytest.mark.parametrize("context", [None, "remote-builder"])
@pytest.mark.parametrize("admitted", [False, True])
def test_container_context_is_disclosed_and_user_preference_is_accepted(
    toolbox: Path,
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    context: str | None,
    admitted: bool,
) -> None:
    from enginery.cli.factory_setup import load_project_configuration

    class _Hold:
        message = "no qualified container engine is reachable"
        reason = HoldReason.RUNNER_UNQUALIFIED

    class _Decision:
        hold = None if admitted else _Hold()

    class _Admission:
        def __init__(self, **_: object) -> None: ...

        def admit(self, selection: object) -> _Decision:
            return _Decision()

    monkeypatch.setattr(factory_diagnostics, "OciRunnerAdmission", _Admission)
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "isolated-oci"\n'
        f'image = "sha256:{"a" * 64}"\n[validation]\ncommands = ["python3 -m unittest"]\n'
    )
    if context is not None:
        user = Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(f'schema_version = 1\n[runner]\ndocker_context = "{context}"\n')
    configuration = load_project_configuration(project)
    assert configuration.compiled is not None
    assert configuration.compiled.runner.docker_context == context
    code, report = _doctor(capsys, project)
    assert code == (0 if admitted else HOLD)
    checks = cast(list[dict[str, str]], report["checks"])
    runner = next(item for item in checks if item["name"] == "container-runner")
    context_label = f"Docker context {context!r}" if context is not None else "default context"
    assert context_label in runner["detail"]
    network = cast(list[str], report["network_access"])
    assert f"container engine contact for {context_label}" in network
    if not admitted:
        resolve = runner["resolve"]
        assert "docker_context in [runner] in $XDG_CONFIG_HOME/enginery/user.toml" in resolve
        assert "enginery stage1 build-request --docker-context <name>" in resolve
        assert ".enginery/project.toml" not in resolve
        assert "factory doctor --docker-context" not in resolve
