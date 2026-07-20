"""Tests for enginery.engine.fixture_build."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.application.delivery_ports import ReleaseArtifact
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.engine.fixture_build import BuiltFixtureArtifacts, FixtureBuilder


def _throwaway_package(tmp_path: Path, *, name: str = "throwaway-fixture") -> Path:
    root = tmp_path / "pkg"
    module_dir = root / "src" / "throwaway_fixture"
    module_dir.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f"""[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/throwaway_fixture"]
""",
        encoding="utf-8",
    )
    (module_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    return root


def test_build_and_verify_clean_install_round_trip(tmp_path: Path) -> None:
    """A genuine `uv build` + `uv venv` + `uv pip install` round trip, not a fake."""
    root = _throwaway_package(tmp_path)
    builder = FixtureBuilder()

    artifacts = builder.build(root, expected_version="0.1.0")

    assert artifacts.wheel_path.exists()
    assert artifacts.sdist_path.exists()
    assert artifacts.wheel.version == "0.1.0"
    assert artifacts.sdist.version == "0.1.0"

    builder.verify_clean_install(
        artifacts, import_module="throwaway_fixture", expected_version="0.1.0"
    )


def test_verify_clean_install_rejects_a_version_mismatch(tmp_path: Path) -> None:
    root = _throwaway_package(tmp_path)
    builder = FixtureBuilder()
    artifacts = builder.build(root, expected_version="0.1.0")

    with pytest.raises(ExternalConflictError, match="does not match"):
        builder.verify_clean_install(
            artifacts, import_module="throwaway_fixture", expected_version="0.2.0"
        )


class _FakeRunner:
    def __init__(self, results: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self._results = results
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(command))
        key = command[1] if len(command) > 1 else command[0]
        return self._results.get(key, subprocess.CompletedProcess(tuple(command), 0))


def _ok(command: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_build_raises_when_the_build_command_fails(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    runner = _FakeRunner({"build": subprocess.CompletedProcess((), 1, stdout="", stderr="boom")})
    builder = FixtureBuilder(command_runner=runner)

    with pytest.raises(ExternalConflictError, match="build failed"):
        builder.build(root, expected_version="0.1.0")


def test_build_raises_when_no_artifacts_are_produced(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    runner = _FakeRunner({})  # uv build "succeeds" but writes nothing to dist/
    builder = FixtureBuilder(command_runner=runner)

    with pytest.raises(InternalInvariantViolationError, match="exactly one wheel"):
        builder.build(root, expected_version="0.1.0")


def test_build_raises_when_artifact_filenames_carry_the_wrong_version(tmp_path: Path) -> None:
    root = tmp_path / "pkg"

    def fake_build(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        dist = cwd / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "throwaway_fixture-0.9.0-py3-none-any.whl").write_bytes(b"wheel-bytes")
        (dist / "throwaway_fixture-0.9.0.tar.gz").write_bytes(b"sdist-bytes")
        return subprocess.CompletedProcess(tuple(command), 0)

    builder = FixtureBuilder(command_runner=fake_build)

    with pytest.raises(ExternalConflictError, match="expected version"):
        builder.build(root, expected_version="0.1.0")


def test_build_requires_an_absolute_fixture_root() -> None:
    builder = FixtureBuilder(command_runner=_FakeRunner({}))

    with pytest.raises(InvalidInputError):
        builder.build(Path("relative"), expected_version="0.1.0")


def test_verify_clean_install_raises_when_the_wheel_does_not_install(tmp_path: Path) -> None:
    artifacts = BuiltFixtureArtifacts(
        wheel=_fake_artifact(),
        wheel_path=tmp_path / "throwaway_fixture-0.1.0-py3-none-any.whl",
        sdist=_fake_artifact(),
        sdist_path=tmp_path / "throwaway_fixture-0.1.0.tar.gz",
    )
    artifacts.wheel_path.write_bytes(b"not-a-real-wheel")
    runner = _FakeRunner(
        {
            "venv": _ok(),
            "pip": subprocess.CompletedProcess((), 1, stdout="", stderr="install failed"),
        }
    )
    builder = FixtureBuilder(command_runner=runner)

    with pytest.raises(ExternalConflictError, match="did not install cleanly"):
        builder.verify_clean_install(
            artifacts, import_module="throwaway_fixture", expected_version="0.1.0"
        )


def _fake_artifact() -> ReleaseArtifact:
    return ReleaseArtifact(
        version="0.1.0", digest=Digest.of_bytes(b"x"), media_type="application/vnd.pypa.wheel"
    )
