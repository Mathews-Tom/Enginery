from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.cli.test_stage1_request_builder import _git, _repository

from enginery.adapters.git_reader import git_bytes
from enginery.domain.errors import InvalidInputError


def test_reader_ignores_replace_objects_and_ambient_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base = _repository(tmp_path)
    (repository / "README").write_text("real change\n")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "real", cwd=repository)
    head = _git("rev-parse", "HEAD", cwd=repository)
    _git("replace", head, base, cwd=repository)
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    assert git_bytes(repository, "show", f"{head}:README") == b"real change\n"


def test_reader_kills_a_process_that_never_closes_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(InvalidInputError, match="timed out"):
        git_bytes(tmp_path, "status", timeout=0.05)


def test_reader_environment_refuses_transports_and_ambient_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "git"
    executable.write_text(
        f"#!{sys.executable}\nimport os\n"
        "assert os.environ['GIT_ALLOW_PROTOCOL'] == ''\n"
        "assert os.environ['GIT_CONFIG_GLOBAL'] == os.devnull\n"
        "assert os.environ['GIT_CONFIG_SYSTEM'] == os.devnull\n"
        "assert os.environ['GIT_NO_REPLACE_OBJECTS'] == '1'\n"
        "assert os.environ['GIT_NO_LAZY_FETCH'] == '1'\n"
        "assert 'ENGINERY_TEST_SECRET' not in os.environ\n"
        "print('safe')\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("ENGINERY_TEST_SECRET", "not-a-real-secret")
    assert git_bytes(tmp_path, "status") == b"safe\n"


def test_reader_refuses_oversized_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "git"
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 1048577)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(InvalidInputError, match="1 MiB"):
        git_bytes(tmp_path, "status")
