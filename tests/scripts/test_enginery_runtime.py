"""Real offline venv installs and rollback, using tiny deterministic stdlib-only wheels."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "enginery_runtime.py"


@dataclass(frozen=True)
class RuntimeHome:
    home: Path
    env: dict[str, str]

    @property
    def root(self) -> Path:
        return self.home / "data" / "enginery" / "runtimes"

    @property
    def shim(self) -> Path:
        return self.home / ".local" / "bin" / "enginery"

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *arguments],
            input="",
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.home.parent,
        )

    def install(self, wheel: Path) -> subprocess.CompletedProcess[str]:
        return self.run("install", str(wheel), "--yes-sha256", _sha256(wheel), "--no-index")

    def listing(self) -> dict[str, object]:
        result = self.run("list", "--json")
        assert result.returncode == 0, result.stderr
        return cast(dict[str, object], json.loads(result.stdout))

    def version(self) -> str:
        result = subprocess.run(
            [str(self.shim), "--version"],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return result.stdout.strip()


@pytest.fixture
def runtime_home(tmp_path: Path) -> RuntimeHome:
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return RuntimeHome(home, env)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wheel(
    directory: Path,
    version: str,
    *,
    schema: int = 1,
    reported_version: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"enginery-{version}-py3-none-any.whl"
    dist_info = f"enginery-{version}.dist-info"
    files = {
        "enginery/__init__.py": "",
        "enginery/cli.py": (f"def main():\n    print('enginery {reported_version or version}')\n"),
        "enginery/ledger/__init__.py": "",
        "enginery/ledger/schema.py": (
            "from types import SimpleNamespace\n"
            f"MIGRATIONS = (SimpleNamespace(version={schema}),)\n"
        ),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: enginery\nVersion: {version}\n",
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: runtime-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": "[console_scripts]\nenginery = enginery.cli:main\n",
    }
    record = f"{dist_info}/RECORD"
    files[record] = "".join(f"{name},,\n" for name in [*files, record])
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(zipfile.ZipInfo(name), content)
    return path


def _ledger(path: Path, version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES (?)", (version,))


def test_confirmation_is_required_before_any_mutation(
    runtime_home: RuntimeHome, tmp_path: Path
) -> None:
    wheel = _wheel(tmp_path / "wheels", "1.0")
    for extra in [[], ["--yes-sha256", "0" * 64]]:
        result = runtime_home.run("install", str(wheel), "--no-index", *extra)
        assert result.returncode == 5, result.stderr
        assert "Version: 1.0" in result.stdout
        assert _sha256(wheel) in result.stdout
        assert not runtime_home.home.exists()


@pytest.mark.parametrize("variable", ["XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"])
def test_relative_xdg_is_usage_error_without_mutation(
    runtime_home: RuntimeHome, variable: str
) -> None:
    env = {**runtime_home.env, variable: "relative"}
    result = RuntimeHome(runtime_home.home, env).run("list", "--json")
    assert result.returncode == 2
    assert variable in result.stderr
    assert "absolute" in result.stderr
    assert not runtime_home.home.exists()


def test_install_list_and_repeatable_rollback(runtime_home: RuntimeHome, tmp_path: Path) -> None:
    first = _wheel(tmp_path / "first", "1.0")
    second = _wheel(tmp_path / "second", "2.0")
    for wheel, expected in [(first, "enginery 1.0"), (second, "enginery 2.0")]:
        installed = runtime_home.install(wheel)
        assert installed.returncode == 0, installed.stderr
        assert runtime_home.version() == expected
    listing = runtime_home.listing()
    current = listing["current"]
    previous = listing["previous"]
    assert isinstance(current, dict)
    assert isinstance(previous, list)
    assert current["version"] == "2.0"
    assert current["sha256"] == _sha256(second)
    assert previous[0]["sha256"] == _sha256(first)
    assert current["id"] != previous[0]["id"]
    assert all(current[key] for key in ["wheel", "installed_at", "python"])
    text = runtime_home.run("list")
    assert text.returncode == 0
    assert "current:" in text.stdout and "previous:" in text.stdout
    assert _sha256(first) in text.stdout and _sha256(second) in text.stdout
    fresh_ledger = runtime_home.home / "state/enginery/projects/fresh/ledger.sqlite"
    fresh_ledger.parent.mkdir(parents=True)
    with sqlite3.connect(fresh_ledger) as connection:
        connection.execute("CREATE TABLE fresh (value INTEGER)")
    fresh_bytes = fresh_ledger.read_bytes()
    for expected in ["enginery 1.0", "enginery 2.0"]:
        rollback = runtime_home.run("rollback")
        assert rollback.returncode == 0, rollback.stderr
        assert runtime_home.version() == expected
    assert fresh_ledger.read_bytes() == fresh_bytes
    assert len(list(runtime_home.root.glob("*/receipt.json"))) == 2


def test_rollback_checks_every_explicit_and_real_xdg_ledger_read_only(
    runtime_home: RuntimeHome, tmp_path: Path
) -> None:
    first = _wheel(tmp_path / "first", "1.0", schema=1)
    second = _wheel(tmp_path / "second", "2.0", schema=3)
    for wheel in [first, second]:
        installed = runtime_home.install(wheel)
        assert installed.returncode == 0, installed.stderr
    discovered = (
        runtime_home.home / "state" / "enginery" / "projects" / "project-id" / "ledger.sqlite"
    )
    explicit = tmp_path / "external.sqlite"
    compatible = tmp_path / "compatible.sqlite"
    for ledger, version in [(discovered, 3), (explicit, 2), (compatible, 1)]:
        _ledger(ledger, version)
    ledgers = [discovered, explicit, compatible]
    before = {path: path.read_bytes() for path in ledgers}
    state_before = (runtime_home.root / "state.json").read_bytes()
    shim_before = runtime_home.shim.readlink()
    result = runtime_home.run("rollback", "--ledger", str(compatible), "--ledger", str(explicit))
    assert result.returncode == 5, result.stderr
    for ledger, version in [(discovered, 3), (explicit, 2)]:
        assert str(ledger) in result.stderr
        previous_runtime = runtime_home.root / f"1.0-{_sha256(first)[:12]}"
        assert (
            f"{previous_runtime}/venv/bin/enginery operator restore --database {ledger} "
            "--backup <dir> --force-unpaused"
        ) in result.stderr
        assert "Pause and drain" in result.stderr
        assert "supports 1" in result.stderr
        assert f"current schema {version}" in result.stderr
    assert runtime_home.version() == "enginery 2.0"
    assert runtime_home.shim.readlink() == shim_before
    assert (runtime_home.root / "state.json").read_bytes() == state_before
    assert {path: path.read_bytes() for path in ledgers} == before
    assert all(not Path(f"{path}-journal").exists() for path in ledgers)


def test_failed_install_removes_partial_runtime_and_preserves_activation(
    runtime_home: RuntimeHome, tmp_path: Path
) -> None:
    first = _wheel(tmp_path / "first", "1.0")
    installed = runtime_home.install(first)
    assert installed.returncode == 0, installed.stderr
    before = (runtime_home.root / "state.json").read_bytes()
    shim_before = runtime_home.shim.readlink()
    invalid = _wheel(tmp_path / "invalid", "2.0", reported_version="9.0")
    result = runtime_home.install(invalid)
    assert result.returncode == 5, result.stderr
    assert "version mismatch" in result.stderr
    assert runtime_home.version() == "enginery 1.0"
    assert runtime_home.shim.readlink() == shim_before
    assert (runtime_home.root / "state.json").read_bytes() == before
    assert not (runtime_home.root / f"2.0-{_sha256(invalid)[:12]}").exists()


def test_install_refuses_to_replace_an_enginery_it_does_not_manage(
    runtime_home: RuntimeHome, tmp_path: Path
) -> None:
    foreign = tmp_path / "uv-tools" / "enginery"
    foreign.parent.mkdir()
    foreign.write_text("#!/bin/sh\necho 'enginery 0.4.0'\n", encoding="utf-8")
    foreign.chmod(0o755)
    runtime_home.shim.parent.mkdir(parents=True)
    runtime_home.shim.symlink_to(foreign)
    result = runtime_home.install(_wheel(tmp_path / "wheels", "1.0"))
    assert result.returncode == 5, result.stderr
    assert "not manage" in result.stderr
    assert runtime_home.shim.readlink() == foreign
    assert not runtime_home.root.exists()


def test_install_confirms_and_installs_its_private_copy(
    runtime_home: RuntimeHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import enginery_runtime

    wheel = _wheel(tmp_path / "wheels", "1.0")
    digest = _sha256(wheel)
    for variable in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(variable, runtime_home.env[variable])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def confirm(_prompt: str) -> str:
        wheel.write_bytes(b"the source changed while the operator reviewed the hash")
        return "yes"

    monkeypatch.setattr("builtins.input", confirm)
    assert enginery_runtime.main(["install", str(wheel), "--no-index"]) == 0
    assert runtime_home.version() == "enginery 1.0"
    copied = runtime_home.root / f"1.0-{digest[:12]}" / wheel.name
    assert _sha256(copied) == digest
    assert _sha256(wheel) != digest
