"""The in-image wrapper echoes the launcher's per-attempt nonce in its result."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

SCRIPT = Path(str(resources.files("enginery.adapters.oci").joinpath("assets/worker_entry.py")))
_NONCE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _run(tmp_path: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    workspace = tmp_path / "workspace"
    out = tmp_path / "out"
    workspace.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--operation-id",
            "op-1",
            "--result",
            "result.json",
            "--deadline-seconds",
            "30",
            "--workspace",
            str(workspace),
            "--out",
            str(out),
            *extra,
            "--",
            sys.executable,
            "-c",
            "print('hello')",
        ],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return completed, out / "result.json"


def test_the_result_echoes_the_nonce_it_was_started_with(tmp_path: Path) -> None:
    completed, result = _run(tmp_path, "--nonce", _NONCE)

    assert completed.returncode == 0, completed.stderr
    document = json.loads(result.read_text("utf-8"))
    assert document["nonce"] == _NONCE
    assert document["operation_id"] == "op-1"
    assert document["terminal_status"] == "succeeded"
    assert set(document) == {"nonce", "operation_id", "output", "schema_version", "terminal_status"}


def test_the_nonce_is_echoed_when_the_command_fails_too(tmp_path: Path) -> None:
    completed, result = _run(tmp_path, "--nonce", _NONCE, "--seed-file", "/nonexistent-dir/x=!!")

    assert completed.returncode == 1
    document = json.loads(result.read_text("utf-8"))
    assert document["terminal_status"] == "failed"
    assert document["nonce"] == _NONCE


def test_a_result_written_without_a_nonce_carries_none(tmp_path: Path) -> None:
    completed, result = _run(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "nonce" not in json.loads(result.read_text("utf-8"))


def test_a_blank_nonce_is_refused(tmp_path: Path) -> None:
    completed, result = _run(tmp_path, "--nonce", "  ")

    assert completed.returncode == 2
    assert "--nonce must be non-blank" in completed.stderr
    assert not result.exists()


@pytest.mark.parametrize("flag", ["--nonce"])
def test_the_nonce_flag_takes_a_value(tmp_path: Path, flag: str) -> None:
    completed, _ = _run(tmp_path, flag)

    assert completed.returncode == 2
