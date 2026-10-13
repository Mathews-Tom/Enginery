"""In-container entrypoint for an isolated worker: run one command, hand results to the host.

This file is copied into the worker image and runs under the image's ``python3`` (3.11+). It
uses only the standard library and imports nothing from the host package, so it cannot reach
anything the container does not already have.

Behaviour, in order:

1. ``umask 000`` so every file it creates stays deletable by the host operator, including when
   the engine maps container uids to unprivileged sub-uids.
2. Seed files (``--seed-file ABSPATH=BASE64``) are written with mode 0600 before the command.
3. The command runs in a new process group with no stdin and stdout+stderr merged. A deadline
   sends SIGTERM to the whole group, then SIGKILL after a grace period.
4. When a bundle was requested, ``git bundle create BASE..BRANCH`` packages the committed work
   for the host. A bundle failure is logged to stderr and never changes the terminal status.
5. The result JSON (``operation_id``, ``output``, ``schema_version``, ``terminal_status``) is
   written atomically. Output is *not* redacted here; the host redacts on collection.

The process exit code is the command's return code, or ``128 + signal`` when it was killed by a
signal; a deadline that ended in a clean exit still exits 124.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

RESULT_SCHEMA_VERSION = 1
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_OUT = "/out"
DEFAULT_BUNDLE_NAME = "branch.bundle"
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_KILL_GRACE_SECONDS = 10.0
BUNDLE_TIMEOUT_SECONDS = 600.0
DEADLINE_EXIT_CODE = 124
_POLL_SECONDS = 0.1
_READ_CHUNK = 64 * 1024


class _OutputCapture:
    """Drain a pipe, keeping the first and last halves of an oversized stream."""

    def __init__(self, descriptor: int, limit: int) -> None:
        self._descriptor = descriptor
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail: collections.deque[bytes] = collections.deque()
        self._tail_size = 0
        self._omitted = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._descriptor, _READ_CHUNK)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._append(chunk)

    def _append(self, chunk: bytes) -> None:
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail.append(chunk)
        self._tail_size += len(chunk)
        while self._tail_size > self._tail_limit:
            excess = self._tail_size - self._tail_limit
            oldest = self._tail[0]
            if len(oldest) <= excess:
                self._tail.popleft()
                self._tail_size -= len(oldest)
                self._omitted += len(oldest)
            else:
                self._tail[0] = oldest[excess:]
                self._tail_size -= excess
                self._omitted += excess

    def finish(self, timeout: float) -> str:
        """Wait for end-of-stream (bounded) and return the captured text."""
        self._thread.join(timeout)
        with self._lock:
            tail = b"".join(self._tail)
            if self._omitted:
                marker = f"\n[enginery worker: {self._omitted} bytes of output omitted]\n"
                raw = bytes(self._head) + marker.encode("utf-8") + tail
            else:
                raw = bytes(self._head) + tail
        return raw.decode("utf-8", errors="replace")


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` via a same-directory temp file and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _encode_result(operation_id: str, terminal_status: str, output: str) -> bytes:
    """Serialize the result, shrinking ``output`` until the file fits the host's size cap."""
    while True:
        document = {
            "operation_id": operation_id,
            "output": output,
            "schema_version": RESULT_SCHEMA_VERSION,
            "terminal_status": terminal_status,
        }
        encoded = (
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if len(encoded) <= MAX_RESULT_BYTES:
            return encoded
        keep = int(len(output) * (MAX_RESULT_BYTES / len(encoded)) * 0.9)
        half = keep // 2
        omitted = len(output) - 2 * half
        output = (
            output[:half]
            + f"\n[enginery worker: {omitted} characters of output omitted]\n"
            + output[len(output) - half :]
        )


def _write_result(path: Path, operation_id: str, terminal_status: str, output: str) -> None:
    _write_atomic(path, _encode_result(operation_id, terminal_status, output))


def _write_seed_files(seeds: Sequence[str]) -> None:
    for entry in seeds:
        raw_path, separator, encoded = entry.partition("=")
        if not separator or not os.path.isabs(raw_path):
            raise ValueError("seed file must be ABSOLUTE_PATH=BASE64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"seed file {raw_path} is not valid base64") from None
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)


def _signal_group(process_group: int, signal_number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process_group, signal_number)


def _run_command(
    command: Sequence[str], *, deadline_seconds: float, kill_grace_seconds: float
) -> tuple[str, int, str | None]:
    """Run ``command``; return ``(output, exit_code, deadline_note)``.

    ``deadline_note`` is set when the deadline or a termination request ended the run early.
    """
    interrupted = threading.Event()

    def _request_stop(_signal_number: int, _frame: object) -> None:
        interrupted.set()

    previous = {
        number: signal.signal(number, _request_stop) for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            return str(error), 1, None
        assert process.stdout is not None
        capture = _OutputCapture(process.stdout.fileno(), MAX_OUTPUT_BYTES)
        started = time.monotonic()
        note: str | None = None
        while process.poll() is None:
            if interrupted.is_set():
                note = "termination requested"
            elif time.monotonic() - started >= deadline_seconds:
                note = f"deadline of {deadline_seconds:g} seconds exceeded"
            if note is not None:
                _signal_group(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(kill_grace_seconds)
                _signal_group(process.pid, signal.SIGKILL)
                process.wait()
                break
            time.sleep(_POLL_SECONDS)
        # Whatever the command left behind in its group would hold the output pipe open.
        _signal_group(process.pid, signal.SIGKILL)
        output = capture.finish(timeout=10.0)
        process.stdout.close()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    returncode = process.returncode
    if note is not None:
        if output and not output.endswith("\n"):
            output += "\n"
        output += f"[enginery worker: {note}; process group terminated]\n"
        if returncode == 0:
            return output, DEADLINE_EXIT_CODE, note
    exit_code = returncode if returncode >= 0 else 128 + abs(returncode)
    return output, exit_code, note


def _create_bundle(workspace: Path, bundle_path: Path, base: str, branch: str) -> None:
    """Package ``base..branch``; any failure is logged and leaves no bundle file behind."""
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.unlink(missing_ok=True)
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "safe.directory=*",
                "-C",
                str(workspace),
                "bundle",
                "create",
                str(bundle_path),
                f"{base}..{branch}",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            env=environment,
            timeout=BUNDLE_TIMEOUT_SECONDS,
        )
        failure = None if completed.returncode == 0 else f"git exited {completed.returncode}"
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = str(error)
    if failure is not None:
        bundle_path.unlink(missing_ok=True)
        print(f"enginery worker: bundle was not created: {failure}", file=sys.stderr)


def _parse_arguments(arguments: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    wrapper = list(arguments)
    command: list[str] = []
    if "--" in wrapper:
        split = wrapper.index("--")
        wrapper, command = wrapper[:split], wrapper[split + 1 :]
    parser = argparse.ArgumentParser(prog="worker_entry.py")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--deadline-seconds", required=True, type=float)
    parser.add_argument("--bundle-branch")
    parser.add_argument("--bundle-base")
    parser.add_argument("--bundle-path")
    parser.add_argument("--seed-file", action="append", default=[])
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--kill-grace-seconds", type=float, default=DEFAULT_KILL_GRACE_SECONDS)
    parsed = parser.parse_args(wrapper)
    if not parsed.operation_id.strip():
        parser.error("--operation-id must be non-blank")
    if not parsed.deadline_seconds > 0:
        parser.error("--deadline-seconds must be positive")
    if parsed.kill_grace_seconds < 0:
        parser.error("--kill-grace-seconds must not be negative")
    if (parsed.bundle_branch is None) != (parsed.bundle_base is None):
        parser.error("--bundle-branch and --bundle-base must be given together")
    if parsed.bundle_path is not None and parsed.bundle_branch is None:
        parser.error("--bundle-path requires --bundle-branch")
    if not command or any(not value for value in command):
        parser.error("a non-blank command is required after '--'")
    return parsed, command


def main(arguments: Sequence[str] | None = None) -> int:
    parsed, command = _parse_arguments(sys.argv[1:] if arguments is None else arguments)
    os.umask(0)
    out_dir = Path(parsed.out)
    result_path = out_dir / parsed.result
    try:
        _write_seed_files(parsed.seed_file)
    except (OSError, ValueError) as error:
        _write_result(result_path, parsed.operation_id, "failed", f"seed file failed: {error}\n")
        return 1
    output, exit_code, _note = _run_command(
        command,
        deadline_seconds=parsed.deadline_seconds,
        kill_grace_seconds=parsed.kill_grace_seconds,
    )
    if parsed.bundle_branch is not None:
        bundle_path = out_dir / (parsed.bundle_path or DEFAULT_BUNDLE_NAME)
        _create_bundle(
            Path(parsed.workspace), bundle_path, parsed.bundle_base, parsed.bundle_branch
        )
    _write_result(
        result_path,
        parsed.operation_id,
        "succeeded" if exit_code == 0 else "failed",
        output,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
