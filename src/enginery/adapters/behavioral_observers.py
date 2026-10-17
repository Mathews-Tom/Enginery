"""Bounded, argv-only command and loopback HTTP/browser observations.

These adapters return measurements, never acceptance verdicts. Trusted-local
commands are operator-authored and are not OS-contained; process-group cleanup
cannot contain a descendant which deliberately starts a new session.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from enginery.adapters.browser_pipe import (
    MAX_BYTES,
    BrowserObserverError,
    loopback_url,
    observe_browser,
    qualify_browser,
)
from enginery.application.behavioral_checks import BehavioralCheck
from enginery.application.behavioral_observation import ObservationData
from enginery.application.runner_ports import AttestedProcess, IsolatedCommandRunner
from enginery.domain.errors import WorkerFailureError

OBSERVATION_TIMEOUT_SECONDS = 30.0


class _ObservationError(Exception):
    """Infrastructure unavailable; never classified as a behavioral assertion failure."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _decode(data: bytes) -> str:
    """Refuse undecodable text instead of expanding replacement bytes past the bound."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _ObservationError(
            "observer_output_undecodable: configure the local check to emit UTF-8 text"
        ) from error


def _require_unused_service_port(url: str) -> None:
    """Do not mistake an already-running, possibly stale server for our child."""
    target = urlsplit(loopback_url(url))
    host = target.hostname
    assert host is not None
    port = target.port or (443 if target.scheme == "https" else 80)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.1)
        if probe.connect_ex((host, port)) == 0:
            raise _ObservationError(
                "observer_service_conflict: stop the existing loopback listener "
                "or configure an unused port before starting the declared server"
            )


def _environment(home: Path) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(
            (
                str(Path(sys.executable).parent),
                "/usr/bin",
                "/bin",
                "/usr/local/bin",
                "/opt/homebrew/bin",
            )
        ),
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class _Process:
    """Drain both pipes concurrently with a shared hard byte ceiling."""

    def __init__(self, argv: tuple[str, ...], cwd: Path, environment: dict[str, str]) -> None:
        if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
            raise _ObservationError("observer_command_invalid: configure a nonempty trusted argv")
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.overflow = threading.Event()
        self._lock = threading.Lock()
        self._size = 0
        self.threads: list[threading.Thread] = []
        streams = ((self.process.stdout, self.stdout), (self.process.stderr, self.stderr))
        for stream, target in streams:
            assert stream is not None
            thread = threading.Thread(target=self._read, args=(stream, target), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _read(self, stream: BinaryIO, target: bytearray) -> None:
        try:
            while chunk := os.read(stream.fileno(), 8192):
                with self._lock:
                    remaining = MAX_BYTES - self._size
                    target.extend(chunk[:remaining])
                    self._size += min(remaining, len(chunk))
                    if len(chunk) > remaining:
                        self.overflow.set()
                        self.kill()
                        return
        except OSError:
            return

    def kill(self) -> None:
        # macOS reports EPERM for a group whose members have all exited but are unreaped.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)

    def check(self) -> None:
        if self.overflow.is_set():
            raise _ObservationError("observer_output_limit: stdout/stderr exceed 1 MiB")

    def wait(self, deadline: float) -> None:
        while self.process.poll() is None:
            self.check()
            if time.monotonic() >= deadline:
                raise _ObservationError(
                    "observer_timeout: command exceeded its observation deadline"
                )
            time.sleep(0.01)
        self.check()

    def close(self) -> None:
        self.kill()
        self.process.wait()
        for thread in self.threads:
            thread.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _probe(url: str, *, deadline: float, process: _Process | None) -> tuple[int, str]:
    target = loopback_url(url)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    while time.monotonic() < deadline:
        if process is not None:
            process.check()
        remaining = deadline - time.monotonic()
        try:
            response = opener.open(
                Request(target, headers={"Accept-Encoding": "identity"}),
                timeout=min(1.0, remaining),
            )
        except HTTPError as error:
            response = error
        except (URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            if process is not None and process.process.poll() is not None:
                raise _ObservationError(
                    "observer_service_unavailable: server exited before its loopback probe"
                ) from None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        with response:
            status = response.getcode()
            if 300 <= status < 400:
                raise _ObservationError(
                    "observer_redirect_refused: loopback probes never follow redirects"
                )
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() >= deadline:
                    raise _ObservationError(
                        "observer_timeout: service body exceeded its observation deadline"
                    )
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise _ObservationError("observer_output_limit: service body exceeds 1 MiB")
                chunks.append(chunk)
            return status, _decode(b"".join(chunks))
    raise _ObservationError("observer_timeout: loopback service did not become ready")


def _produced_files(check: BehavioralCheck, cwd: Path) -> tuple[tuple[str, str], ...]:
    """Read only declared regular files through no-follow, directory-relative fds."""
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    total = 0
    for assertion in check.assertions:
        name = assertion.path
        if assertion.kind != "produced_file" or name is None or name in seen:
            continue
        seen.add(name)
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in name:
            raise _ObservationError(
                "observer_produced_file_unsafe: require a relative path inside cwd"
            )
        descriptors: list[int] = []
        try:
            directory = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(directory)
            for component in path.parts[:-1]:
                directory = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                descriptors.append(directory)
            descriptor = os.open(
                path.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise _ObservationError(
                    "observer_produced_file_unsafe: links and non-regular files are refused"
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, min(65536, MAX_BYTES + 1 - total)):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise _ObservationError("observer_output_limit: produced files exceed 1 MiB")
                chunks.append(chunk)
            values.append((name, _decode(b"".join(chunks))))
        except FileNotFoundError:
            continue  # Absence is an evaluator-owned assertion outcome.
        except OSError as error:
            raise _ObservationError(
                "observer_produced_file_unsafe: cannot read a link-safe candidate file"
            ) from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    return tuple(values)


def _require_fresh_outputs(check: BehavioralCheck, cwd: Path) -> None:
    for assertion in check.assertions:
        if assertion.kind != "produced_file" or assertion.path is None:
            continue
        path = PurePosixPath(assertion.path)
        if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in assertion.path:
            raise _ObservationError("observer_produced_file_unsafe: require a relative output path")
        target = cwd
        for part in path.parts:
            target /= part
            if target.is_symlink():
                raise _ObservationError("observer_produced_file_unsafe: linked output path")
        if target.exists():
            if target.stat().st_nlink != 1:
                raise _ObservationError("observer_produced_file_unsafe: linked output file")
            raise _ObservationError(
                "behavioral_output_preexists: remove the declared output from the committed "
                "candidate; the observer requires a freshly produced file"
            )


# This wrapper runs only inside the existing network-none validation container.
# Temporary files bound collection without buffering arbitrary candidate output in memory.
_ISOLATED_CAPTURE = r"""
import json, subprocess, sys, tempfile
limit = 1024 * 1024
try:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.run(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=err, timeout=30, check=False)
        out.seek(0); err.seek(0)
        stdout, stderr = out.read(limit + 1), err.read(limit + 1)
        if len(stdout) + len(stderr) > limit:
            raise ValueError("observer_output_limit")
        payload = json.dumps({"returncode": process.returncode,
                              "stdout": stdout.decode(), "stderr": stderr.decode()})
        if len(payload.encode()) > limit:
            raise ValueError("observer_output_limit")
        print(payload)
except (OSError, ValueError, subprocess.TimeoutExpired) as error:
    print(json.dumps({"error": "observer_isolated_capture_failed",
                      "detail": type(error).__name__}))
"""


def _isolated_observation(
    check: BehavioralCheck, cwd: Path, runner: IsolatedCommandRunner | None
) -> ObservationData:
    start = _timestamp()
    data = ObservationData(observer=check.observer, started_at=start, ran=False)
    try:
        if check.observer != "command" or runner is None:
            raise _ObservationError(
                "behavioral_observer_requires_isolation: isolated runs require the pinned "
                "network-none command observer; configure a command check and rebuild the "
                "request, or deliberately start a new trusted-local run"
            )
        _require_fresh_outputs(check, cwd)
        result = runner(("python3", "-I", "-c", _ISOLATED_CAPTURE, json.dumps(check.argv)), cwd)
        if not isinstance(result, AttestedProcess):
            raise _ObservationError(
                "behavioral_observer_receipt_missing: restore the isolated runner"
            )
        data = replace(data, runner_receipt_digest=result.runner_receipt.receipt_digest)
        if result.returncode != 0 or len(result.stdout.encode()) > MAX_BYTES:
            raise _ObservationError(
                "observer_isolated_capture_failed: repair the pinned runner image"
            )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("capture is not an object")
        code, stdout, stderr = (
            payload.get("returncode"),
            payload.get("stdout"),
            payload.get("stderr"),
        )
        if type(code) is not int or not isinstance(stdout, str) or not isinstance(stderr, str):
            raise ValueError("capture lacks bounded command results")
        data = replace(
            data,
            ran=True,
            returncode=code,
            stdout=stdout,
            stderr=stderr,
            produced_files=_produced_files(check, cwd),
        )
    except _ObservationError as error:
        data = replace(data, infrastructure_error=str(error))
    except WorkerFailureError:
        data = replace(
            data,
            infrastructure_error=(
                "observer_isolated_capture_failed: repair the pinned isolated runner and retry"
            ),
        )
    except (OSError, ValueError):
        data = replace(data, infrastructure_error="observer_isolated_capture_failed")
    return replace(data, ended_at=_timestamp())


def observe(
    check: BehavioralCheck,
    *,
    cwd: Path,
    browser_executable: str | None = None,
    browser_expected_digest: str | None = None,
    isolated: bool = False,
    command_runner: IsolatedCommandRunner | None = None,
) -> ObservationData:
    """Measure a command, service, or qualified browser under a bounded deadline.

    Expected launch, path, network, timeout and CDP failures are returned as
    ``infrastructure_error`` (not raised, not assertion failures). Programming
    errors and cancellation are not hidden. Optional service/browser argv starts
    a server, which is torn down with its whole process group on every exit.
    """
    if isolated:
        return _isolated_observation(check, cwd, command_runner)
    observation = ObservationData(observer=check.observer, started_at=_timestamp(), ran=False)
    process: _Process | None = None
    failure: str | None = None
    deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
    try:
        if not cwd.is_absolute() or cwd.is_symlink() or not cwd.is_dir():
            raise _ObservationError(
                "observer_cwd_invalid: provide a verified absolute candidate directory"
            )
        if check.observer not in {"command", "service", "browser"}:
            raise _ObservationError("observer_kind_invalid: configure command, service or browser")
        _require_fresh_outputs(check, cwd)
        executable = None
        if check.observer == "browser":
            executable = qualify_browser(browser_executable, browser_expected_digest)
        if check.observer != "command":
            loopback_url(check.url or "")
            if check.argv:
                _require_unused_service_port(check.url or "")
        with tempfile.TemporaryDirectory(prefix="enginery-observer-") as temporary:
            home = Path(temporary)
            home.chmod(0o700)
            environment = _environment(home)
            try:
                if check.argv:
                    process = _Process(check.argv, cwd, environment)
                    observation = replace(observation, ran=True)
                if check.observer == "command":
                    if process is None:
                        raise _ObservationError("observer_command_invalid: a command requires argv")
                    process.wait(deadline)
                    observation = replace(observation, returncode=process.process.returncode)
                else:
                    status, body = _probe(check.url or "", deadline=deadline, process=process)
                    observation = replace(observation, ran=True, returncode=status, body=body)
                    if check.observer == "browser":
                        assert executable is not None
                        dom, png = observe_browser(
                            check,
                            executable=executable,
                            environment=environment,
                            deadline=deadline,
                        )
                        observation = replace(
                            observation, dom_results=dom, captures=(("image/png", png),)
                        )
            finally:
                if process is not None:
                    process.close()
                    observation = replace(
                        observation,
                        stdout=_decode(bytes(process.stdout)),
                        stderr=_decode(bytes(process.stderr)),
                    )
                    process.check()
            observation = replace(observation, produced_files=_produced_files(check, cwd))
    except (_ObservationError, BrowserObserverError) as error:
        failure = str(error)
    except FileNotFoundError:
        failure = (
            "observer_tool_missing: install the declared executable "
            "or repair its operator-authored argv"
        )
    except PermissionError:
        failure = "observer_tool_unavailable: repair executable permissions or candidate access"
    except (OSError, ValueError, http.client.HTTPException):
        failure = "observer_transport_failed: repair the declared local observer and retry"
    return replace(observation, ended_at=_timestamp(), infrastructure_error=failure)
