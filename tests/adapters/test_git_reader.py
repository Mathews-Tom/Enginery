from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.cli.test_request_compiler import _git, _repository

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


def test_review_diff_keeps_text_renames_gitlinks_and_real_objects(tmp_path: Path) -> None:
    from enginery.adapters.git_reader import DIFF_FLAGS
    from enginery.cli.factory_review import _git as review_git

    repository, _ = _repository(tmp_path)
    protected = repository / ".github"
    protected.mkdir()
    (protected / "workflow.yml").write_text("sensitive\n")
    (repository / ".gitattributes").write_text("README -diff\n")
    _git("add", ".", cwd=repository)
    _git("commit", "-m", "protected fixture", cwd=repository)
    first = _git("rev-parse", "HEAD", cwd=repository)
    _git("update-index", "--add", "--cacheinfo", f"160000,{first},module", cwd=repository)
    _git("commit", "-m", "gitlink fixture", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    (repository / "README").write_text("visible despite binary attribute\n")
    _git("mv", ".github/workflow.yml", "ordinary.yml", cwd=repository)
    _git("update-index", "--cacheinfo", f"160000,{base},module", cwd=repository)
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "candidate", cwd=repository)
    head = _git("rev-parse", "HEAD", cwd=repository)
    _git("replace", head, base, cwd=repository)
    _git("config", "diff.ignoreSubmodules", "all", cwd=repository)
    _git("config", "submodule.module.ignore", "all", cwd=repository)
    diff = review_git(repository, "diff", *DIFF_FLAGS, base, head, "--")
    assert "+visible despite binary attribute" in diff
    assert "Subproject commit" in diff
    assert "deleted file mode" in diff
    paths = review_git(repository, "diff", "--name-only", "-z", *DIFF_FLAGS, base, head, "--")
    assert ".github/workflow.yml" in paths.split("\0")
    assert "ordinary.yml" in paths.split("\0")
    assert "module" in paths.split("\0")
