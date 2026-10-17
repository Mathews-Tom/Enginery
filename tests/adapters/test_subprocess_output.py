"""Shared output bounds apply to both streams without exposing diagnostics."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from enginery.adapters.subprocess_output import (
    SubprocessOutputError,
    SubprocessResult,
    execute_output,
    execute_result,
)


def test_stdout_and_working_directory(tmp_path: Path) -> None:
    assert execute_output(
        (sys.executable, "-c", "import os; print(os.getcwd())"), dict(os.environ), cwd=tmp_path
    ).strip() == str(tmp_path)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_combined_output_is_bounded(stream: str) -> None:
    with pytest.raises(SubprocessOutputError, match="exceeds 1 MiB"):
        execute_output(
            (sys.executable, "-c", f"import sys; sys.{stream}.write('x' * (1024 * 1024 + 1))"),
            dict(os.environ),
        )


def test_timeout_is_bounded() -> None:
    with pytest.raises(SubprocessOutputError, match="timed out"):
        execute_output(
            (sys.executable, "-c", "import time; time.sleep(10)"), dict(os.environ), timeout=0.05
        )


def test_nonzero_diagnostics_are_redacted() -> None:
    with pytest.raises(SubprocessOutputError, match="status 7") as failure:
        execute_output(
            (sys.executable, "-c", "import sys; sys.stderr.write('secret'); sys.exit(7)"),
            dict(os.environ),
        )
    assert "secret" not in str(failure.value)


def test_invalid_utf8_is_refused() -> None:
    with pytest.raises(SubprocessOutputError, match="not UTF-8"):
        execute_output(
            (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"),
            dict(os.environ),
        )


@pytest.mark.parametrize("code", [0, 7])
def test_result_returns_exit_status_and_both_streams(tmp_path: Path, code: int) -> None:
    result = execute_result(
        (
            sys.executable,
            "-c",
            f"import os, sys; print(os.getcwd()); sys.stderr.write('secret'); sys.exit({code})",
        ),
        dict(os.environ),
        cwd=tmp_path,
    )
    assert result == SubprocessResult(code, str(tmp_path) + "\n", "secret")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_result_nonzero_output_is_bounded(stream: str) -> None:
    with pytest.raises(SubprocessOutputError, match="exceeds 1 MiB") as failure:
        execute_result(
            (
                sys.executable,
                "-c",
                f"import sys; sys.{stream}.write('secret' * 200000); sys.exit(7)",
            ),
            dict(os.environ),
        )
    assert "secret" not in str(failure.value)


def test_result_combined_limit_applies_to_both_streams() -> None:
    with pytest.raises(SubprocessOutputError, match="exceeds 1 MiB"):
        execute_result(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 600000); sys.stdout.flush(); "
                "sys.stderr.write('y' * 600000)",
            ),
            dict(os.environ),
        )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_result_nonzero_invalid_utf8_is_refused(stream: str) -> None:
    with pytest.raises(SubprocessOutputError, match="not UTF-8") as failure:
        execute_result(
            (
                sys.executable,
                "-c",
                f"import sys; sys.{stream}.buffer.write(b'secret\\xff'); sys.exit(7)",
            ),
            dict(os.environ),
        )
    assert "secret" not in str(failure.value)


def test_result_timeout_redacts_stderr() -> None:
    with pytest.raises(SubprocessOutputError, match="timed out") as failure:
        execute_result(
            (
                sys.executable,
                "-c",
                "import sys, time; sys.stderr.write('secret'); sys.stderr.flush(); time.sleep(10)",
            ),
            dict(os.environ),
            timeout=0.1,
        )
    assert "secret" not in str(failure.value)


def test_result_startup_failure_is_redacted(tmp_path: Path) -> None:
    with pytest.raises(SubprocessOutputError, match="could not be started"):
        execute_result((str(tmp_path / "missing"),), dict(os.environ))


@pytest.mark.parametrize("argv", [("",), ("bad\0argument",), ("x" * (1024 * 1024),)])
def test_result_invalid_argv_is_refused(argv: tuple[str, ...]) -> None:
    with pytest.raises(SubprocessOutputError, match="argv"):
        execute_result(argv, dict(os.environ))
