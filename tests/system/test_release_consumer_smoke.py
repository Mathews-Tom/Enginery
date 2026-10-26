"""Tests for `release_consumer_smoke.py`: the script's own guards must fail when they should.

`exercise_surface` is run against the real installed `enginery` entry point of the
development environment, so the passing case is the real consumer surface and not a
stand-in. The negative cases tamper with one observed fact at a time, or give the script a
launcher that violates exactly one contract, and require the matching guard to reject it.
"""

from __future__ import annotations

import copy
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

import release_consumer_smoke
from release_consumer_smoke import ConsumerSmokeError

_EXECUTABLE = Path(sys.executable).parent / "enginery"
_PYTHON = Path(sys.executable)


def _version() -> str:
    return metadata.version("enginery")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"wheel": Path("dist/enginery-0.6.0-py3-none-any.whl")}, "0.6.0"),
        ({"sdist": Path("dist/enginery-0.6.0.tar.gz")}, "0.6.0"),
        ({"package": "enginery==0.6.0"}, "0.6.0"),
        ({"package": "enginery==0.7.0"}, "0.7.0"),
        ({"wheel": Path("dist/enginery-1.2.3rc1-py3-none-any.whl")}, "1.2.3rc1"),
    ],
)
def test_expected_version_is_derived_from_the_artifact_identity(
    kwargs: dict[str, object], expected: str
) -> None:
    arguments: dict[str, object] = {"wheel": None, "sdist": None, "package": None, **kwargs}
    assert release_consumer_smoke.expected_version(**arguments) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wheel": Path("dist/other-0.6.0-py3-none-any.whl")},
        {"sdist": Path("dist/enginery-0.6.0.zip")},
        {"package": "enginery>=0.6.0"},
        {"package": "enginery"},
    ],
)
def test_expected_version_rejects_an_unrecognised_artifact(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {"wheel": None, "sdist": None, "package": None, **kwargs}
    with pytest.raises(ConsumerSmokeError, match="cannot derive the expected version"):
        release_consumer_smoke.expected_version(**arguments)  # type: ignore[arg-type]


def test_an_artifact_source_is_required_and_exclusive() -> None:
    with pytest.raises(SystemExit) as no_source:
        release_consumer_smoke.main([])
    assert no_source.value.code == 2
    with pytest.raises(SystemExit) as two_sources:
        release_consumer_smoke.main(["--wheel", "a.whl", "--sdist", "b.tar.gz"])
    assert two_sources.value.code == 2


def test_a_missing_sdist_is_rejected_before_anything_is_installed(tmp_path: Path) -> None:
    with pytest.raises(ConsumerSmokeError, match="artifact does not exist"):
        release_consumer_smoke.main(["--sdist", str(tmp_path / "enginery-0.6.0.tar.gz")])


def test_real_editable_surface_is_not_installed_workflow_proof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ConsumerSmokeError, match="outside consumer venv"):
        release_consumer_smoke.exercise_surface(
            executable=_EXECUTABLE, python=_PYTHON, root=tmp_path, version=_version()
        )
    assert (tmp_path / "request-claude-code.json").is_file()
    assert (tmp_path / "request-omp.json").is_file()
    assert not (tmp_path / "rejected-request.json").exists()
    output = capsys.readouterr().out.splitlines()
    observed = [line for line in output if line.startswith("ok ")]
    # The dev environment is an editable install, so the installed package *is* the source tree
    # and the digest comparison is reported NOT RUN (it runs for real against a wheel install).
    expected_fragments = [
        "enginery --version",
        "enginery doctor",
        "help",
        "consolidated project/engine/operator help",
        "unsupported pins exit 2",
        "packaged execution catalog",
        "valid pins write requests",
        "gate status --gate G4 remains fail-closed",
        "runner --help, runner status --help and operator --help",
        "engine build-request lists --runner, --runner-image and --model-access",
        "packaged worker and relay recipe files are present",
    ]
    assert len(observed) == len(expected_fragments)
    for line, fragment in zip(observed, expected_fragments, strict=True):
        assert fragment in line
    skipped = [line for line in output if "NOT RUN" in line]
    assert sum("recipe digests equal" in line for line in skipped) == 1


@pytest.fixture
def consolidated_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    overrides: dict[tuple[str, ...], tuple[int, str, str]] = {}

    def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        args = tuple(command[1:])
        if args in overrides:
            code, stdout, stderr = overrides[args]
        elif command[0] == str(_PYTHON):
            code, stdout, stderr = 0, '["enginery"]', ""
        elif args[:-1] in release_consumer_smoke._REMOVED_COMMANDS:
            path = args[:-1]
            removed = path[1] if path[0] == "engine" else path[0]
            code, stdout, stderr = 2, "", f"invalid choice: '{removed}'"
        else:
            group = args[:-1]
            code, stdout, stderr = (
                0,
                " ".join(release_consumer_smoke._CONSOLIDATED_COMMANDS.get(group, ())),
                "",
            )
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    monkeypatch.setattr(release_consumer_smoke, "_run", run)
    return overrides


@pytest.mark.parametrize("path", release_consumer_smoke._REMOVED_COMMANDS)
def test_accepted_obsolete_command_is_rejected(
    tmp_path: Path,
    consolidated_runner: dict[tuple[str, ...], tuple[int, str, str]],
    path: tuple[str, ...],
) -> None:
    consolidated_runner[(*path, "--help")] = (0, "old command", "")
    with pytest.raises(ConsumerSmokeError, match="removed command"):
        release_consumer_smoke._check_consolidated_surface(_EXECUTABLE, _PYTHON, root=tmp_path)


@pytest.mark.parametrize("code,stderr", [(1, "invalid choice: 'stage1'"), (2, "unrelated")])
def test_obsolete_command_requires_real_unknown_command_exit_and_message(
    tmp_path: Path,
    consolidated_runner: dict[tuple[str, ...], tuple[int, str, str]],
    code: int,
    stderr: str,
) -> None:
    consolidated_runner[("stage1", "--help")] = (code, "", stderr)
    with pytest.raises(ConsumerSmokeError, match="removed command"):
        release_consumer_smoke._check_consolidated_surface(_EXECUTABLE, _PYTHON, root=tmp_path)


@pytest.mark.parametrize("group", [(), ("engine",), ("operator",)])
def test_missing_consolidated_command_is_rejected(
    tmp_path: Path,
    consolidated_runner: dict[tuple[str, ...], tuple[int, str, str]],
    group: tuple[str, ...],
) -> None:
    consolidated_runner[(*group, "--help")] = (0, "usage: nothing", "")
    with pytest.raises(ConsumerSmokeError, match="consolidated help missing"):
        release_consumer_smoke._check_consolidated_surface(_EXECUTABLE, _PYTHON, root=tmp_path)


def test_an_extra_console_script_is_rejected(
    tmp_path: Path,
    consolidated_runner: dict[tuple[str, ...], tuple[int, str, str]],
) -> None:
    script = tmp_path / "enginery-operator"
    script.touch()
    with pytest.raises(ConsumerSmokeError, match="exactly one console script"):
        release_consumer_smoke._check_consolidated_surface(
            tmp_path / "enginery", _PYTHON, root=tmp_path
        )


def test_an_extra_metadata_console_script_is_rejected(
    tmp_path: Path,
    consolidated_runner: dict[tuple[str, ...], tuple[int, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = release_consumer_smoke._run

    def extra_script(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        result = run(command, cwd=cwd)
        if command[0] == str(_PYTHON):
            return subprocess.CompletedProcess(command, 0, '["enginery", "enginery-operator"]', "")
        return result

    monkeypatch.setattr(release_consumer_smoke, "_run", extra_script)
    assert not (tmp_path / "enginery-operator").exists()
    with pytest.raises(ConsumerSmokeError, match="exactly one console script"):
        release_consumer_smoke._check_consolidated_surface(
            tmp_path / "enginery", _PYTHON, root=tmp_path
        )


def test_a_doctor_reporting_another_version_fails_the_doctor_check(tmp_path: Path) -> None:
    launcher = _launcher(
        tmp_path,
        'case "$1" in\n'
        "  --version) echo 'enginery 1.2.3' ;;\n"
        "  doctor) echo '[ok] package_metadata: enginery 0.0.1 installed' ;;\n"
        "esac\n"
        "exit 0",
    )
    release_consumer_smoke._check_version(launcher, root=tmp_path, version="1.2.3")
    with pytest.raises(ConsumerSmokeError, match="did not report"):
        release_consumer_smoke._check_doctor(launcher, root=tmp_path, version="1.2.3")


@pytest.mark.parametrize(
    "body",
    [
        'echo \'{"overall": "fail"}\'; exit 0',
        'echo \'{"overall": "pass"}\'; exit 3',
        "exit 3",
    ],
)
def test_a_g4_status_must_both_exit_3_and_report_fail(tmp_path: Path, body: str) -> None:
    launcher = _launcher(tmp_path, body)
    with pytest.raises(ConsumerSmokeError, match="G4 did not remain fail-closed"):
        release_consumer_smoke._check_g4_fail_closed(launcher, root=tmp_path)


def test_a_g4_status_that_exits_3_and_reports_fail_is_accepted(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, 'echo \'{"overall": "fail"}\'; exit 3')
    release_consumer_smoke._check_g4_fail_closed(launcher, root=tmp_path)


@pytest.mark.parametrize(
    "override",
    [
        {"hosts": {"omp": "supported", "claude-code": "experimental"}},
        {"hosts": {"omp": "experimental"}},
        {"baseline_version": "2"},
        {"journeys": 0},
        {"check_ids": []},
    ],
)
def test_unexpected_packaged_data_fails_the_packaged_data_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: dict[str, object]
) -> None:
    real = release_consumer_smoke._packaged_data(_PYTHON, root=tmp_path)
    monkeypatch.setattr(
        release_consumer_smoke, "_packaged_data", lambda python, *, root: {**real, **override}
    )
    with pytest.raises(ConsumerSmokeError):
        release_consumer_smoke._check_packaged_data(_PYTHON, root=tmp_path)


def test_a_wrong_expected_version_fails_the_surface(tmp_path: Path) -> None:
    with pytest.raises(ConsumerSmokeError, match="--version"):
        release_consumer_smoke.exercise_surface(
            executable=_EXECUTABLE, python=_PYTHON, root=tmp_path, version="0.0.0"
        )


@pytest.fixture(scope="module")
def valid_request(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("valid-request")
    output = root / "request.json"
    command = [
        *release_consumer_smoke._build_request_command(_EXECUTABLE, root=root, output=output),
        "--harness-executable",
        str(release_consumer_smoke._stand_in_host(root, "claude-code", "2.1.300")),
        "--harness-provider",
        "claude-code",
        "--model",
        "claude-sonnet-4-5",
        "--effort",
        "high",
    ]
    release_consumer_smoke._require_success(command, cwd=root)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _check(document: dict[str, object]) -> None:
    packaged = release_consumer_smoke._packaged_data(_PYTHON, root=Path.cwd())
    release_consumer_smoke.check_bound_selection(
        document,
        harness="claude-code",
        model="claude-sonnet-4-5",
        effort="high",
        catalog_version=str(packaged["catalog_version"]),
        catalog_digest=str(packaged["catalog_digest"]),
        accepted_versions=">=2.1.296,<3.0.0",
        tested_version="2.1.296",
        observed_version="2.1.300",
    )


def test_a_real_pinned_request_is_accepted_as_bound(valid_request: dict[str, object]) -> None:
    _check(valid_request)


def _tamper(document: dict[str, object], path: tuple[str, ...], value: object) -> dict[str, object]:
    tampered = copy.deepcopy(document)
    node: dict[str, object] = tampered
    for key in path[:-1]:
        child = node[key]
        assert isinstance(child, dict)
        node = child
    node[path[-1]] = value
    return tampered


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("execution_selection", "model", "pinned"), False),
        (("execution_selection", "model", "selected"), "other-model"),
        (("execution_selection", "effort", "value"), "low"),
        (("execution_selection", "harness", "value"), "omp"),
        (("execution_selection", "mode"), "host_default"),
        (("execution_selection", "strict_effective"), True),
        (("execution_selection", "catalog", "digest"), "sha256:" + "0" * 64),
        (("execution_selection", "host", "support_tier"), "supported"),
        (("execution_selection", "host", "executable_versions"), ">=2.1.296,<4.0.0"),
        (("execution_selection", "host", "tested_executable_version"), "2.1.300"),
        (("execution_selection", "host", "observed_executable_version"), "2.1.296"),
        (("execution_selection", "host", "observed_executable_version"), None),
        (("execution_configuration", "harness_provider"), "omp"),
    ],
)
def test_check_bound_selection_rejects_each_unbound_fact(
    valid_request: dict[str, object], path: tuple[str, ...], value: object
) -> None:
    with pytest.raises(ConsumerSmokeError):
        _check(_tamper(valid_request, path, value))


def test_check_bound_selection_rejects_a_request_without_a_selection(
    valid_request: dict[str, object],
) -> None:
    stripped = {k: v for k, v in valid_request.items() if k != "execution_selection"}
    with pytest.raises(ConsumerSmokeError, match="no execution_selection"):
        _check(stripped)


def _launcher(directory: Path, body: str) -> Path:
    path = directory / "enginery-stub"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_an_accepting_launcher_fails_the_rejected_pin_check(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, "exit 0")
    with pytest.raises(ConsumerSmokeError, match="not rejected with exit code 2"):
        release_consumer_smoke._check_rejected_pins(launcher, root=tmp_path)


def test_a_rejection_that_still_writes_a_file_fails_the_pin_check(tmp_path: Path) -> None:
    launcher = _launcher(
        tmp_path,
        'while [ "$#" -gt 0 ]; do [ "$1" = --output ] && out="$2"; shift; done\n'
        'echo "opus gpt-5 strict" >&2; echo x > "$out"; exit 2',
    )
    with pytest.raises(ConsumerSmokeError, match="still wrote"):
        release_consumer_smoke._check_rejected_pins(launcher, root=tmp_path)


def test_a_rejection_that_does_not_name_the_pin_fails_the_pin_check(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, "echo unrelated >&2; exit 2")
    with pytest.raises(ConsumerSmokeError, match="without naming"):
        release_consumer_smoke._check_rejected_pins(launcher, root=tmp_path)


def test_a_g4_status_that_passes_fails_the_fail_closed_check(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, 'echo \'{"overall": "pass"}\'; exit 0')
    with pytest.raises(ConsumerSmokeError, match="G4 did not remain fail-closed"):
        release_consumer_smoke._check_g4_fail_closed(launcher, root=tmp_path)


def test_a_help_without_the_pin_flags_fails_the_help_check(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, 'echo "usage: --model --effort"; exit 0')
    with pytest.raises(ConsumerSmokeError, match="--strict-effective"):
        release_consumer_smoke._check_help(launcher, root=tmp_path)


def test_a_printed_digest_that_differs_from_the_request_fails_the_pin_check(
    tmp_path: Path,
) -> None:
    launcher = _launcher(
        tmp_path,
        f'out=$("{_EXECUTABLE}" "$@") || exit $?\n'
        'echo "$out" | sed \'s/"request_digest": "sha256:/"request_digest": "sha256:0/\'',
    )
    packaged = release_consumer_smoke._packaged_data(_PYTHON, root=tmp_path)
    with pytest.raises(ConsumerSmokeError, match="differs from its printed report"):
        release_consumer_smoke._check_valid_pins(launcher, root=tmp_path, packaged=packaged)


def test_the_packaged_stand_in_versions_are_in_range_and_never_the_tested_version(
    tmp_path: Path,
) -> None:
    versions = release_consumer_smoke._packaged_data(_PYTHON, root=tmp_path)["versions"]

    assert isinstance(versions, dict)
    assert set(versions) == {"omp", "claude-code"}
    for host in versions.values():
        assert host["stand_in"] != host["tested"]


def test_a_stand_in_that_equals_the_tested_version_fails_the_pin_check(
    tmp_path: Path,
) -> None:
    real = release_consumer_smoke._packaged_data(_PYTHON, root=tmp_path)
    versions = {
        harness: {**host, "stand_in": host["tested"]}  # type: ignore[dict-item]
        for harness, host in real["versions"].items()  # type: ignore[attr-defined]
    }

    with pytest.raises(ConsumerSmokeError, match="other than the tested"):
        release_consumer_smoke._check_valid_pins(
            _EXECUTABLE, root=tmp_path, packaged={**real, "versions": versions}
        )


# --- the isolated-runner surface -------------------------------------------------------------

_ASSET_MEMBERS = (
    "Dockerfile.worker",
    "worker_entry.py",
    "checkout_entry.py",
    "runtime-requirements.txt",
    "package.json",
    "package-lock.json",
    "omp-isolated-policy.yml",
    "Dockerfile.relay",
    "relay.py",
)
_SOURCE_ASSETS = (
    Path(__file__).resolve().parents[2] / "src" / "enginery" / "adapters" / "oci" / "assets"
)


def _wheel_with(directory: Path, *, omit: str | None = None, tamper: str | None = None) -> Path:
    wheel = directory / "enginery-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in _ASSET_MEMBERS:
            if name == omit:
                continue
            content = (_SOURCE_ASSETS / name).read_bytes()
            if name == tamper:
                content += b"# tampered\n"
            archive.writestr(f"enginery/adapters/oci/assets/{name}", content)
    return wheel


def _source_copy(directory: Path) -> Path:
    source = directory / "src"
    shutil.copytree(
        release_consumer_smoke._SOURCE_ROOT,
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return source


def test_the_installed_recipes_equal_the_source_tree_and_the_wheel_carries_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = _wheel_with(tmp_path)
    not_run = release_consumer_smoke._check_runner_recipes(
        _PYTHON, root=tmp_path, source_root=_source_copy(tmp_path), wheel=wheel
    )
    printed = capsys.readouterr().out
    assert not_run == []
    assert "installed recipe digests equal the source tree's" in printed
    assert "carries every recipe file, byte-identical" in printed


def test_an_editable_install_is_reported_not_run_not_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_run = release_consumer_smoke._check_runner_recipes(
        _PYTHON, root=tmp_path, source_root=release_consumer_smoke._SOURCE_ROOT, wheel=None
    )
    assert len(not_run) == 1 and "editable install" in not_run[0]
    assert "ok installed recipe digests equal" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"omit": "relay.py"}, "lacks enginery/adapters/oci/assets/relay.py"),
        ({"omit": "checkout_entry.py"}, "lacks enginery/adapters/oci/assets/checkout_entry.py"),
        (
            {"omit": "runtime-requirements.txt"},
            "lacks enginery/adapters/oci/assets/runtime-requirements.txt",
        ),
        ({"tamper": "Dockerfile.worker"}, "differs from the source"),
    ],
)
def test_a_wheel_missing_or_altering_a_recipe_file_fails(
    tmp_path: Path, kwargs: dict[str, str], message: str
) -> None:
    wheel = _wheel_with(tmp_path, **kwargs)
    with pytest.raises(ConsumerSmokeError, match=message):
        release_consumer_smoke._check_runner_recipes(
            _PYTHON, root=tmp_path, source_root=release_consumer_smoke._SOURCE_ROOT, wheel=wheel
        )


def test_a_source_tree_with_a_different_recipe_fails_the_digest_comparison(
    tmp_path: Path,
) -> None:
    source = _source_copy(tmp_path)
    dockerfile = source / "enginery" / "adapters" / "oci" / "assets" / "Dockerfile.worker"
    dockerfile.write_text(dockerfile.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(
        ConsumerSmokeError, match=r"recipe digest .* differs from the source tree's"
    ):
        release_consumer_smoke._check_runner_recipes(
            _PYTHON, root=tmp_path, source_root=source, wheel=None
        )


def test_without_a_source_tree_the_digest_comparison_is_reported_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_run = release_consumer_smoke._check_runner_recipes(
        _PYTHON, root=tmp_path, source_root=tmp_path / "no-src", wheel=None
    )
    printed = capsys.readouterr().out
    assert len(not_run) == 1 and not_run[0].startswith("NOT RUN recipe digests")
    assert "NOT RUN" in printed
    assert "installed recipe digests equal" not in printed


def test_an_operator_help_failure_is_reported(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, 'if [ "$1" = operator ]; then exit 2; fi')
    with pytest.raises(ConsumerSmokeError, match="operator"):
        release_consumer_smoke._check_runner_help(launcher, root=tmp_path)


def test_an_operator_help_without_its_commands_fails(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, 'echo "usage: enginery operator"')
    with pytest.raises(ConsumerSmokeError, match="does not list"):
        release_consumer_smoke._check_runner_help(launcher, root=tmp_path)


def test_a_help_without_the_runner_flags_fails_the_stage1_check(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path, 'echo "usage: --model --effort --runner"; exit 0')
    with pytest.raises(ConsumerSmokeError, match="--runner-image"):
        release_consumer_smoke._check_stage1_runner_flags(launcher, root=tmp_path)


def _engine_probe(directory: Path, *, reachable: bool) -> Path:
    path = directory / "engine-probe"
    body = (
        "echo qualified; exit 0"
        if reachable
        else "echo 'MissingPrerequisiteError: no engine'; exit 3"
    )
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_an_unreachable_engine_is_reported_not_run_never_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _launcher(tmp_path, "exit 99")
    not_run = release_consumer_smoke._check_runner_engine(
        launcher, _engine_probe(tmp_path, reachable=False), root=tmp_path
    )
    printed = capsys.readouterr().out
    assert len(not_run) == 1 and not_run[0].startswith("engine NOT RUN")
    assert "engine ok" not in printed


def test_a_reachable_engine_must_hold_an_absent_image(tmp_path: Path) -> None:
    holding = _launcher(
        tmp_path,
        'echo \'{"admitted": false, "hold": {"reason": "runner_unqualified"}}\'; exit 5',
    )
    probe = _engine_probe(tmp_path, reachable=True)
    assert release_consumer_smoke._check_runner_engine(holding, probe, root=tmp_path) == []
    admitting = _launcher(tmp_path, 'echo \'{"admitted": true, "hold": null}\'; exit 0')
    with pytest.raises(ConsumerSmokeError, match="did not hold an absent image"):
        release_consumer_smoke._check_runner_engine(admitting, probe, root=tmp_path)
    silent = _launcher(tmp_path, "exit 5")
    with pytest.raises(ConsumerSmokeError, match="printed no report"):
        release_consumer_smoke._check_runner_engine(silent, probe, root=tmp_path)


def test_an_unqualified_engine_is_distinguished_from_an_unreachable_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _launcher(tmp_path, "exit 99")
    unqualified = tmp_path / "unqualified-probe"
    unqualified.write_text(
        "#!/bin/sh\necho 'unqualified: MissingPrerequisiteError: missing cgroup v2'; exit 4\n",
        encoding="utf-8",
    )
    unqualified.chmod(unqualified.stat().st_mode | stat.S_IXUSR)
    not_run = release_consumer_smoke._check_runner_engine(launcher, unqualified, root=tmp_path)
    assert len(not_run) == 1 and "(unqualified)" in not_run[0] and "unreachable" not in not_run[0]
    unreachable = release_consumer_smoke._check_runner_engine(
        launcher, _engine_probe(tmp_path, reachable=False), root=tmp_path
    )
    assert "(unreachable)" in unreachable[0] and "(unqualified)" not in unreachable[0]
    capsys.readouterr()


@pytest.mark.parametrize(
    ("details", "kind", "code"),
    [({"missing": "cgroup v2", "engine": "x"}, "unqualified", 4), ({}, "unreachable", 3)],
)
def test_the_engine_probe_program_classifies_the_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    details: dict[str, object],
    kind: str,
    code: int,
) -> None:
    from enginery.adapters.oci import engine as engine_module
    from enginery.domain.errors import MissingPrerequisiteError

    class _Engine:
        def qualify(self) -> None:
            raise MissingPrerequisiteError("no", details=details)

    monkeypatch.setattr(engine_module, "OciEngine", _Engine)
    with pytest.raises(SystemExit) as raised:
        exec(release_consumer_smoke._ENGINE_PROGRAM, {"__name__": "probe"})
    assert raised.value.code == code
    assert capsys.readouterr().out.startswith(kind + ":")


def test_an_incomplete_run_never_prints_pass(capsys: pytest.CaptureFixture[str]) -> None:
    code = release_consumer_smoke.report_outcome(["engine NOT RUN x", "NOT RUN recipe digests y"])
    printed = capsys.readouterr().out
    assert code == release_consumer_smoke.INCOMPLETE_EXIT != 0
    assert "PASS" not in printed
    assert "INCOMPLETE" in printed and "engine NOT RUN x" in printed
    assert release_consumer_smoke.report_outcome([]) == 0
    assert "PASS release-consumer-smoke" in capsys.readouterr().out


def test_a_wheel_is_claimed_byte_identical_only_when_it_was_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = _wheel_with(tmp_path)
    not_run = release_consumer_smoke._check_runner_recipes(
        _PYTHON, root=tmp_path, source_root=tmp_path / "no-src", wheel=wheel
    )
    printed = capsys.readouterr().out
    assert len(not_run) == 2
    assert any(item.startswith("NOT RUN wheel recipe files are byte-identical") for item in not_run)
    claims = [line for line in printed.splitlines() if line.startswith("ok ")]
    assert not any("byte-identical" in line for line in claims)
    assert "carries every recipe file" in printed


def test_require_engine_turns_a_skipped_engine_check_into_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_consumer_smoke, "_check_installed_workflow", _unavailable_workflow)
    monkeypatch.setattr(
        release_consumer_smoke,
        "_check_runner_engine",
        lambda *args, **kwargs: ["engine NOT RUN runner status against a container engine"],
    )
    arguments = {"executable": _EXECUTABLE, "python": _PYTHON, "root": tmp_path}
    skipped = release_consumer_smoke.exercise_surface(**arguments, version=_version())
    assert skipped[-1] == "engine NOT RUN runner status against a container engine"
    with pytest.raises(ConsumerSmokeError, match="--require-engine: the container engine was"):
        release_consumer_smoke.exercise_surface(
            **arguments, version=_version(), require_engine=True
        )


@pytest.mark.parametrize("not_run", [[], ["engine NOT RUN (unreachable)"]])
def test_install_receipt_retains_identity_and_incomplete_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, not_run: list[str]
) -> None:
    wheel = tmp_path / "enginery-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"bound artifact")
    output = tmp_path / "receipt.json"

    def successful(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        result = subprocess.CompletedProcess(command, 0, "", "")
        release_consumer_smoke._record_command(command, result)
        return result

    step = {"name": "installed-manual-path", "status": "pass", "assertion_count": 20}

    def surface(**kwargs: object) -> list[str]:
        release_consumer_smoke._STEPS.append(step)
        return not_run

    monkeypatch.setattr(release_consumer_smoke, "_run", successful)
    monkeypatch.setattr(release_consumer_smoke, "exercise_surface", surface)
    code = release_consumer_smoke.main(["--wheel", str(wheel), "--install-receipt", str(output)])
    receipt = json.loads(output.read_text())
    assert receipt["artifact"] == {
        "kind": "wheel",
        "name": wheel.name,
        "sha256": release_consumer_smoke._sha256(wheel),
    }
    assert receipt["installed_version"] == "1.2.3"
    assert receipt["entry_point"].endswith("/venv/bin/enginery")
    assert receipt["platform"]["system"]
    assert receipt["python"]["version"]
    assert len(receipt["commands"]) == 2
    assert all(command["exit_code"] == 0 for command in receipt["commands"])
    assert receipt["steps"] == [step]
    assert receipt["not_run"] == not_run
    assert receipt["exit_code"] == code == (3 if not_run else 0)
    assert receipt["outcome"] == ("incomplete" if not_run else "pass")


def test_install_failure_receipt_cannot_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "enginery-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"bound artifact")
    output = tmp_path / "failed.json"

    def failed(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        result = subprocess.CompletedProcess(command, 9, "", "installation failed")
        release_consumer_smoke._record_command(command, result)
        return result

    monkeypatch.setattr(release_consumer_smoke, "_run", failed)
    with pytest.raises(ConsumerSmokeError, match="command failed"):
        release_consumer_smoke.main(["--wheel", str(wheel), "--install-receipt", str(output)])
    receipt = json.loads(output.read_text())
    assert receipt["outcome"] == "fail"
    assert receipt["exit_code"] == 1
    assert receipt["installed_version"] is None
    assert receipt["commands"][0]["exit_code"] == 9


def test_command_receipt_records_expected_nonzero_exit(tmp_path: Path) -> None:
    release_consumer_smoke._COMMANDS.clear()
    result = release_consumer_smoke._run(
        [sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path
    )
    assert result.returncode == 3
    assert release_consumer_smoke._COMMANDS[-1]["exit_code"] == 3


def _unavailable_workflow(*args: object, **kwargs: object) -> list[str]:
    message = "NOT RUN installed manual-path workflow (test has an editable installation)"
    release_consumer_smoke._STEPS.append(
        {"name": "installed-manual-path", "status": "not-run", "reason": message}
    )
    print(message)
    return [message]


@pytest.fixture
def workflow_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    source = tmp_path / "snapshot" / "src"
    source.mkdir(parents=True)
    script = source.parent / "scripts" / "run_factory_gate.py"
    script.parent.mkdir()
    script.touch()
    python = tmp_path / "venv" / "bin" / "python"
    state: dict[str, object] = {
        "python": python,
        "source": source,
        "version": "0.7.0",
        "code": 0,
        "write": True,
        "module": str(python.parent.parent / "lib" / "enginery" / "__init__.py"),
        "document": {
            "scenario": "manual-path",
            "verdict": "pass",
            "assertions": [{"ran": True, "status": "pass"} for _ in range(20)],
        },
    }

    def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        assert command[0] == str(python)
        if command[1] == "-c":
            stdout = json.dumps({"module": state["module"], "version": state["version"]})
            return subprocess.CompletedProcess(command, 0, stdout, "")
        assert command[1:3] == ["-B", str(script)]
        assert command[3:5] == ["--scenario", "manual-path"]
        if state["write"]:
            Path(command[-1]).write_text(json.dumps(state["document"]))
        return subprocess.CompletedProcess(command, int(state["code"]), "", "")  # type: ignore[call-overload]

    monkeypatch.setattr(release_consumer_smoke, "_run", run)
    release_consumer_smoke._STEPS.clear()
    return state


def _workflow(state: dict[str, object], root: Path) -> list[str]:
    return release_consumer_smoke._check_installed_workflow(
        state["python"],
        root=root,
        version="0.7.0",
        source_root=state["source"],  # type: ignore[arg-type]
    )


def test_installed_workflow_retains_import_identity_and_full_evidence(
    tmp_path: Path,
    workflow_runner: dict[str, object],
) -> None:
    assert _workflow(workflow_runner, tmp_path) == []
    step = release_consumer_smoke._STEPS[-1]
    assert step["status"] == "pass"
    assert step["assertion_count"] == 20
    assert step["evidence"] == workflow_runner["document"]
    assert step["evidence_sha256"] == release_consumer_smoke._sha256(
        tmp_path / "installed-manual-path.json"
    )
    assert step["installed"]["module"] == workflow_runner["module"]


@pytest.mark.parametrize(
    "change",
    [
        {"version": "0.6.0"},
        {"module": "/checkout/src/enginery/__init__.py"},
        {"write": False},
        {"code": 3},
        {"document": {"scenario": "manual-path", "verdict": "pass", "assertions": []}},
        {
            "document": {
                "scenario": "manual-path",
                "verdict": "pass",
                "assertions": [{"ran": False, "status": "pass"} for _ in range(20)],
            }
        },
        {
            "document": {
                "scenario": "setup",
                "verdict": "pass",
                "assertions": [{"ran": True, "status": "pass"} for _ in range(20)],
            }
        },
    ],
)
def test_installed_workflow_rejects_unproven_facts(
    tmp_path: Path,
    workflow_runner: dict[str, object],
    change: dict[str, object],
) -> None:
    workflow_runner.update(change)
    with pytest.raises(ConsumerSmokeError):
        _workflow(workflow_runner, tmp_path)
    assert not release_consumer_smoke._STEPS


def test_missing_workflow_source_is_incomplete_not_pass(tmp_path: Path) -> None:
    release_consumer_smoke._STEPS.clear()
    not_run = release_consumer_smoke._check_installed_workflow(
        _PYTHON, root=tmp_path, version=_version(), source_root=None
    )
    assert len(not_run) == 1 and "NOT RUN" in not_run[0]
    assert release_consumer_smoke._STEPS[-1]["status"] == "not-run"
    assert release_consumer_smoke.report_outcome(not_run) == 3


def test_skipped_workflow_step_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_consumer_smoke, "_check_installed_workflow", lambda *a, **kw: [])
    with pytest.raises(ConsumerSmokeError, match="workflow step was skipped"):
        release_consumer_smoke.exercise_surface(
            executable=_EXECUTABLE, python=_PYTHON, root=tmp_path, version=_version()
        )


def test_consumer_commands_remove_inherited_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/checkout/src")
    result = release_consumer_smoke._run(
        [str(_PYTHON), "-c", "import os; print('PYTHONPATH' in os.environ)"], cwd=tmp_path
    )
    assert result.stdout.strip() == "False"
