"""Bounded, redacted local subprocess transport shared by display adapters."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path

MAX_BYTES = 1024 * 1024


class SubprocessOutputError(ValueError):
    """A subprocess could not safely produce bounded UTF-8 output."""


def execute_output(
    argv: tuple[str, ...],
    environment: dict[str, str],
    *,
    timeout: float = 30.0,
    cwd: Path | None = None,
) -> str:
    """Return stdout; cap combined stdout/stderr at 1 MiB and redact diagnostics."""
    if any(not item or "\0" in item for item in argv):
        raise SubprocessOutputError("subprocess argv contains an empty or invalid argument")
    if sum(len(item.encode()) + 1 for item in argv) > MAX_BYTES:
        raise SubprocessOutputError("subprocess argv exceeds 1 MiB")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            shell=False,
            cwd=cwd,
        )
    except OSError as exc:
        raise SubprocessOutputError("subprocess executable could not be started") from exc
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                assert stream is not None
                selector.register(stream, selectors.EVENT_READ, stream is process.stdout)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SubprocessOutputError("subprocess timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(65536, MAX_BYTES + 1 - total))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise SubprocessOutputError("subprocess output exceeds 1 MiB")
                    if key.data:
                        output.extend(chunk)
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise SubprocessOutputError("subprocess timed out") from exc
            if code:
                # Tool diagnostics may contain credentials; never echo them.
                raise SubprocessOutputError(f"subprocess exited with status {code}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SubprocessOutputError("subprocess output is not UTF-8") from exc
