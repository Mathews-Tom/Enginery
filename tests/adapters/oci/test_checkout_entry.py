"""The image's Python entry executes candidate code without an installed distribution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from enginery.adapters.oci import image

_ENTRY = Path(__file__).resolve().parents[3] / "src/enginery/adapters/oci/assets/checkout_entry.py"
_ROOT = Path(__file__).resolve().parents[3]


def test_missing_checkout_is_refused_before_importing_an_installed_enginery(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(_ENTRY), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "enginery-checkout: ./src/enginery is absent; run from an Enginery checkout\n"
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "directory",
        "[project",
        "",
        "project = []",
        '[project]\nname = "enginery"\n',
        '[project]\nversion = "0.7.0"\n',
        '[project]\nname = 42\nversion = "0.7.0"\n',
        '[project]\nname = "enginery"\nversion = []\n',
        '[project]\nname = ""\nversion = "0.7.0"\n',
        '[project]\nname = "enginery"\nversion = " "\n',
        '[project]\nname = "enginery"\nversion = "0.7.0"\n',
        '[project]\nname = "enginery"\nversion = "0.7.0"\n[project.scripts]\nenginery = "main"\n',
    ],
)
def test_unreadable_or_invalid_project_metadata_is_refused(
    tmp_path: Path, contents: str | None
) -> None:
    (tmp_path / "src/enginery").mkdir(parents=True)
    metadata = tmp_path / "pyproject.toml"
    if contents == "directory":
        metadata.mkdir()
    elif contents is not None:
        metadata.write_text(contents)
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(_ENTRY), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "enginery-checkout: ./pyproject.toml lacks a readable [project] name, version "
        "and console script\n"
    )


def test_candidate_code_metadata_argv_and_exit_code_are_used_without_installation(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src/enginery"
    cli = package / "cli"
    cli.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (cli / "__init__.py").write_text("")
    (cli / "main.py").write_text(
        "import json, sys\n"
        "from importlib import metadata\n"
        "def main():\n"
        "    print(json.dumps([metadata.version('Enginery'), sys.argv[1:], __file__, "
        "metadata.metadata('enginery')['Project-URL']]))\n"
        "    return 17\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "enginery"\nversion = "9.8.7"\n'
        '[project.scripts]\nenginery = "enginery.cli.main:main"\n'
        '[project.urls]\nRepository = "https://example.invalid/candidate"\n'
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(_ENTRY), "engine", "--help", "two words"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 17, completed.stderr
    assert json.loads(completed.stdout) == [
        "9.8.7",
        ["engine", "--help", "two words"],
        str(cli / "main.py"),
        "Repository, https://example.invalid/candidate",
    ]
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("argv", [("--version",), ("engine", "--help")])
def test_real_checkout_cli_can_start(argv: tuple[str, ...]) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(_ENTRY), *argv],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("enginery 0.7.0\n" if argv == ("--version",) else "usage:")


def test_checkout_launcher_and_runtime_requirements_are_recipe_inputs() -> None:
    recipe = image.load_recipe()
    assert recipe[image.CHECKOUT_ENTRY_NAME].startswith(b"#!/opt/enginery/runtime/bin/python3.12\n")
    assert recipe[image.RUNTIME_REQUIREMENTS_NAME]
