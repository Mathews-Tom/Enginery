"""Shared output bounds apply to both streams without exposing diagnostics."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from enginery.adapters.subprocess_output import SubprocessOutputError, execute_output


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
