"""Tests for `release_consumer_smoke.py`: the script's own guards must fail when they should.

`exercise_surface` is run against the real installed `enginery` entry point of the
development environment, so the passing case is the real consumer surface and not a
stand-in. The negative cases tamper with one observed fact at a time, or give the script a
launcher that violates exactly one contract, and require the matching guard to reject it.
"""

from __future__ import annotations

import copy
import json
import stat
import sys
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


def test_the_real_consumer_surface_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    release_consumer_smoke.exercise_surface(
        executable=_EXECUTABLE, python=_PYTHON, root=tmp_path, version=_version()
    )
    assert (tmp_path / "request-claude-code.json").is_file()
    assert (tmp_path / "request-omp.json").is_file()
    assert not (tmp_path / "rejected-request.json").exists()
    observed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("ok ")]
    expected_fragments = [
        "enginery --version",
        "enginery doctor",
        "help",
        "unsupported pins exit 2",
        "packaged execution catalog",
        "valid pins write requests",
        "gate status --gate G4 remains fail-closed",
    ]
    assert len(observed) == len(expected_fragments)
    for line, fragment in zip(observed, expected_fragments, strict=True):
        assert fragment in line


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
