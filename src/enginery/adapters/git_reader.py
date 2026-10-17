"""Bounded, offline Git reads of host-owned repositories, never worker clones."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from pathlib import Path

from enginery.adapters.git_capability import git_environment
from enginery.domain.errors import InvalidInputError

MAX_GIT_BYTES = 1024 * 1024
# Match publication's changed-path semantics, including both sides of renames.
DIFF_FLAGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--text",
    "--no-renames",
    "--ignore-submodules=none",
)


def git_bytes(repository: Path, *arguments: str, timeout: float = 15) -> bytes:
    """Read at most 1 MiB with no transports, replacements, hooks or ambient config."""
    environment = git_environment(protocols="")
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    try:
        with subprocess.Popen(
            (
                "git",
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ),
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            expired = threading.Event()

            def kill() -> None:
                expired.set()
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()

            timer = threading.Timer(timeout, kill)
            timer.start()
            try:
                assert process.stdout is not None
                raw = process.stdout.read(MAX_GIT_BYTES + 1)
                if len(raw) > MAX_GIT_BYTES:
                    kill()
                    raise InvalidInputError("Git output exceeds the 1 MiB limit")
                code = process.wait()
                if expired.is_set():
                    raise InvalidInputError("Git read timed out")
                if code != 0:
                    raise InvalidInputError("cannot read host-owned Git repository")
                return bytes(raw)
            finally:
                timer.cancel()
    except OSError as error:
        raise InvalidInputError("cannot read host-owned Git repository") from error
