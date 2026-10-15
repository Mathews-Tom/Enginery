"""The only path by which a package command runs.

A command or hook package runs *only* from its verified, materialized closure
and *only* while an active owned install record covers exactly that closure:
uninstalling revokes the record and with it the authority to run. Immediately
before executing, the materialized directory is re-verified against the lock
entry, so a changed sibling script, reference, or added file is rejected
rather than executed. The bytes that were verified are written to a fresh
owner-only directory and the command runs from that copy, which is removed
afterwards, so a change to the store after verification cannot change the code
that runs.

The command is the manifest's declared argument vector, executed directly
(never through a shell). Its input is a JSON document validated against the
package's declared input schema (a bounded, documented subset; see
``enginery.capabilities.schema``) before anything is spawned. The process gets
a minimal environment, its own session, and bounded stdout and stderr; its
whole process group is killed once the command exits, however it exits, so a
background child does not outlive the run (a descendant that calls ``setsid``
leaves the group and is outside this boundary: trusted-local is not
containment). Python runs with ``-B -s -E`` (and ``PYTHONNOUSERSITE``/``PYTHONSAFEPATH``
are set so a Python started by a shell script is restricted too) and Node under its
permission model limited to reading the closure directory; that restriction applies only
when Node is the top-level process, which therefore cannot start child processes. A
directly executed ``.sh`` script must start with a reviewed shell shebang. Stdout must be
exactly one JSON document and is returned in canonical form; stderr is captured as
diagnostics. Each failure mode has its own error class: schema violation,
timeout, non-zero exit, and invalid or oversized output.

Approval is digest-bound: ``approvals`` must cover the entry under the digest
of the lock being run, the install record must hold the approved closure, and
the lock's compatibility receipt must match the host it runs on.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.compatibility import current_host, evaluate_compatibility
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityCommandFailedError,
    CapabilityCommandNotRunnableError,
    CapabilityCommandOutputError,
    CapabilityCommandTimeoutError,
    CapabilityIncompatibleError,
    CapabilityLicenseHoldError,
    CapabilityNotFoundError,
    CapabilityNotInstalledError,
)
from enginery.capabilities.install import load_owned_manifest
from enginery.capabilities.lock import NO_APPROVALS, ApprovedCapabilities, CapabilityLock
from enginery.capabilities.manifest import PackageKind, require_shell_shebang
from enginery.capabilities.materialize import load_verified_package
from enginery.capabilities.schema import MAX_INSTANCE_DEPTH, exceeds_depth, validate_instance
from enginery.domain.digests import Digest

SAFE_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
_DIAGNOSTIC_CHARACTERS = 4096
_CHUNK_BYTES = 8192


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of one bounded package command."""

    name: str
    version: str
    closure_digest: Digest
    output: object
    canonical_output: str
    stderr: str
    duration_seconds: float
    argv: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "closure_digest": str(self.closure_digest),
            "output": self.output,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 6),
        }


class _BoundedReader:
    """Reads one pipe in a thread, stopping at ``limit + 1`` bytes."""

    def __init__(self, stream: IO[bytes], limit: int, on_overflow: threading.Event) -> None:
        self._descriptor = stream.fileno()
        self._limit = limit
        self._on_overflow = on_overflow
        self.data = bytearray()
        self.overflowed = False
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self._descriptor, _CHUNK_BYTES)
            except OSError:
                return
            if not chunk:
                return
            self.data.extend(chunk)
            if len(self.data) > self._limit:
                self.overflowed = True
                self._on_overflow.set()
                return

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)


def _write_stdin(process: subprocess.Popen[bytes], payload: bytes) -> threading.Thread:
    def write() -> None:
        stdin = process.stdin
        if stdin is None:  # pragma: no cover - always a pipe here
            return
        try:
            stdin.write(payload)
            stdin.close()
        except (BrokenPipeError, OSError):
            return  # the command exited without reading its input

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


# ``os.waitid`` is absent on some platforms (macOS); the constants are read the same way so the
# module imports, and type-checks, everywhere.
_WAITID: Any = getattr(os, "waitid", None)
_P_PID: int = getattr(os, "P_PID", 1)
_WEXITED: int = getattr(os, "WEXITED", 0)
_WNOWAIT: int = getattr(os, "WNOWAIT", 0)


def _has_exited(process: subprocess.Popen[bytes]) -> bool:
    """Whether the leader has exited, without reaping it where the platform allows.

    ``os.waitid`` with ``WNOWAIT`` leaves an exited leader unreaped, so its pid
    (also the process-group id) cannot be reused before the group is ended. Where
    ``os.waitid`` does not exist (macOS), ``poll`` reaps the leader.
    """

    if _WAITID is not None:
        return _WAITID(_P_PID, process.pid, _WEXITED | os.WNOHANG | _WNOWAIT) is not None
    return process.poll() is not None


def _end_group(process: subprocess.Popen[bytes]) -> None:
    """Wait for the leader to exit, end whatever remains of its process group, and reap it.

    With ``os.waitid`` the group is killed while the exited leader is still
    unreaped, so the group id cannot belong to an unrelated process. Without it
    (macOS) the leader is reaped first and a pid reused as a new group leader in
    that instant would be signalled; that platform keeps this residual risk.
    A descendant that left the group with ``setsid`` is outside this boundary.
    """

    if _WAITID is not None:
        _WAITID(_P_PID, process.pid, _WEXITED | _WNOWAIT)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return
    process.wait()
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


@contextlib.contextmanager
def _private_copy(files: Sequence[PackageFile]) -> Iterator[Path]:
    """The verified ``files`` written to a fresh owner-only directory, removed afterwards."""

    root = Path(tempfile.mkdtemp(prefix="enginery-run-"))
    try:
        for item in files:
            destination = root / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(destination, flags, 0o500 if item.executable else 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o555 if item.executable else 0o444)
                handle.write(item.content)
                handle.flush()
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _diagnostic(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")[:_DIAGNOSTIC_CHARACTERS]


def _resolve_argv(
    argv: tuple[str, ...],
    interpreter: str | None,
    directory: Path,
    interpreter_paths: Mapping[str, str],
) -> tuple[str, ...]:
    if interpreter is None:
        return (str(directory / argv[0]), *argv[1:])
    located = interpreter_paths.get(interpreter) or shutil.which(interpreter, path=SAFE_PATH)
    if located is None or not os.path.isabs(located):
        raise CapabilityCommandNotRunnableError(
            "the reviewed interpreter for this command is not available on the host",
            details={"interpreter": interpreter},
        )
    return (located, *_interpreter_flags(interpreter, located, directory), *argv[1:])


_PYTHON_FLAGS = ("-B", "-s", "-E")


def _interpreter_flags(interpreter: str, located: str, directory: Path) -> tuple[str, ...]:
    """Flags that keep an interpreter from loading code outside the package closure.

    Python skips user site-packages and every ``PYTHON*`` variable. Node runs
    under its permission model, allowed to read only the closure directory, so
    a bare ``require()`` cannot reach an ancestor ``node_modules``; a Node too
    old for the permission model is refused rather than run unrestricted.
    """

    if interpreter == "python3":
        return _PYTHON_FLAGS
    if interpreter == "node":
        return _node_permission_flags(located, directory)
    return ()


@functools.cache
def _node_version(located: str) -> tuple[int, int]:
    try:
        completed = subprocess.run(
            [located, "--version"],
            capture_output=True,
            check=False,
            timeout=10,
            env={"PATH": SAFE_PATH},
            stdin=subprocess.DEVNULL,
        )
        version = completed.stdout.decode("ascii", errors="replace").strip().lstrip("v")
        major, minor, *_ = version.split(".")
        return int(major), int(minor)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise CapabilityCommandNotRunnableError(
            "the version of the node interpreter could not be determined",
            details={"interpreter": "node"},
        ) from error


def _node_permission_flags(located: str, directory: Path) -> tuple[str, ...]:
    major, minor = _node_version(located)
    if (major, minor) >= (23, 5):
        flag = "--permission"
    elif major >= 20:
        flag = "--experimental-permission"
    else:
        raise CapabilityCommandNotRunnableError(
            "node must support its permission model (version 20 or newer) to run a package",
            details={"interpreter": "node", "version": f"{major}.{minor}"},
        )
    return (flag, f"--allow-fs-read={directory.resolve()}")


def run_package_command(
    lock: CapabilityLock,
    name: str,
    payload: object,
    *,
    install_root: Path,
    capabilities_root: Path,
    approvals: ApprovedCapabilities = NO_APPROVALS,
    interpreter_paths: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run the declared command of installed package ``name`` with ``payload`` as JSON input.

    The caller must pass a ``lock`` it resolved itself, never one decoded from a
    workspace-writable lockfile: the trust classes recorded in the lock are honored, so a
    lock someone else can edit can promote a package's trust.
    """

    entry = lock.get(name)
    if entry is None:
        raise CapabilityNotFoundError("capability is not part of the lock", details={"name": name})
    active = load_owned_manifest(install_root)
    owned = active.package(name) if active is not None else None
    if active is None or owned is None or active.lock_digest != lock.digest():
        raise CapabilityNotInstalledError(
            "no active install covers this capability and lock",
            details={"name": name, "install_root": str(install_root)},
        )
    if owned.closure_digest != entry.digest:
        raise CapabilityNotInstalledError(
            "the active install records a different closure", details={"name": name}
        )
    if entry.kind not in (PackageKind.COMMAND, PackageKind.HOOK):
        raise _not_runnable(name, entry.kind)
    if not approvals.covers(entry, lock.digest()):
        raise CapabilityApprovalRequiredError(
            "capability requires interactive exact-digest human approval before it can execute",
            details={"name": name, "version": entry.version},
        )
    approved_closure = dict(active.approved_closures).get(name)
    if entry.requires_human_approval() and approved_closure != entry.digest:
        raise CapabilityApprovalRequiredError(
            "the active install did not record an approval of this exact closure",
            details={"name": name, "version": entry.version},
        )
    if entry.redistribution_blocked():
        raise CapabilityLicenseHoldError(
            "capability license is not cleared for redistribution",
            details={"name": name, "license": entry.license},
        )
    current = evaluate_compatibility(entry.compatibility, current_host())
    if current != entry.receipt:
        raise CapabilityIncompatibleError(
            "the lock's compatibility receipt was not recorded for this host",
            details={
                "name": name,
                "recorded": entry.receipt.to_json(),
                "current": current.to_json(),
            },
        )
    if not entry.receipt.compatible:
        raise CapabilityIncompatibleError(
            "capability is incompatible with this host",
            details={"name": name, "reasons": list(entry.receipt.reasons)},
        )
    _, closure, verified = load_verified_package(entry, capabilities_root)
    command = closure.manifest.command
    if command is None:
        raise _not_runnable(name, entry.kind)
    if command.interpreter is None:
        script_file = next((item for item in verified if item.path == command.script), None)
        if script_file is None:
            raise _not_runnable(name, entry.kind)
        require_shell_shebang(script_file.content, script=command.script)
    validate_instance(command.input_schema, payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    # Only the bytes just verified are executed, from a private copy: the store directory is
    # owner-writable, so running from it would let a swap after verification change the code.
    with _private_copy(verified) as directory:
        argv = _resolve_argv(command.argv, command.interpreter, directory, interpreter_paths or {})
        return _execute(
            name=name,
            version=entry.version,
            digest=entry.digest,
            argv=argv,
            directory=directory,
            payload=encoded,
            timeout=command.timeout_seconds,
            limit=command.max_output_bytes,
        )


def _not_runnable(name: str, kind: PackageKind) -> CapabilityCommandNotRunnableError:
    return CapabilityCommandNotRunnableError(
        "package is not a runnable command or hook",
        details={"name": name, "kind": kind.value},
    )


def _execute(
    *,
    name: str,
    version: str,
    digest: Digest,
    argv: tuple[str, ...],
    directory: Path,
    payload: bytes,
    timeout: int,
    limit: int,
) -> CommandResult:
    environment = {
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=directory,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
        start_new_session=True,
    )
    overflow = threading.Event()
    writer = _write_stdin(process, payload)
    assert process.stdout is not None and process.stderr is not None
    stdout = _BoundedReader(process.stdout, limit, overflow)
    stderr = _BoundedReader(process.stderr, limit, overflow)
    deadline = started + timeout
    timed_out = False
    while not _has_exited(process):
        if overflow.is_set():
            _kill_group(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_group(process)
            break
        time.sleep(0.005)
    _end_group(process)
    for reader in (stdout, stderr):
        reader.join(timeout=1)
        if reader.alive:
            # A surviving descendant that left the group still holds the pipe; the group was
            # already ended while its leader was unreaped, so only wait for the reader.
            reader.join(timeout=5)
    writer.join(timeout=1)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    duration = time.monotonic() - started
    diagnostics = _diagnostic(bytes(stderr.data))
    if timed_out:
        raise CapabilityCommandTimeoutError(
            "package command exceeded its timeout",
            details={"name": name, "timeout_seconds": timeout, "stderr": diagnostics},
        )
    if stdout.overflowed or stderr.overflowed:
        raise CapabilityCommandOutputError(
            "package command output exceeded the output limit",
            details={"name": name, "max_output_bytes": limit, "stderr": diagnostics},
        )
    if process.returncode != 0:
        raise CapabilityCommandFailedError(
            "package command exited with a non-zero status",
            details={"name": name, "exit_code": process.returncode, "stderr": diagnostics},
        )
    output = _parse_output(bytes(stdout.data), name=name, diagnostics=diagnostics)
    try:
        canonical = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except RecursionError as error:
        raise CapabilityCommandOutputError(
            "package command stdout is nested too deeply to be a valid JSON document",
            details={"name": name, "stderr": diagnostics},
        ) from error
    return CommandResult(
        name=name,
        version=version,
        closure_digest=digest,
        output=output,
        canonical_output=canonical,
        stderr=diagnostics,
        duration_seconds=duration,
        argv=argv,
    )


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite number {value} is not valid JSON output")


def _parse_output(raw: bytes, *, name: str, diagnostics: str) -> object:
    try:
        output = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except RecursionError as error:
        raise CapabilityCommandOutputError(
            "package command stdout is nested too deeply to be a valid JSON document",
            details={"name": name, "stderr": diagnostics},
        ) from error
    except (UnicodeDecodeError, ValueError) as error:
        raise CapabilityCommandOutputError(
            "package command stdout is not a single valid JSON document",
            details={"name": name, "reason": str(error), "stderr": diagnostics},
        ) from error
    if exceeds_depth(output, MAX_INSTANCE_DEPTH):
        raise CapabilityCommandOutputError(
            "package command stdout is nested too deeply to be a valid JSON document",
            details={"name": name, "limit": MAX_INSTANCE_DEPTH, "stderr": diagnostics},
        )
    return output


__all__ = ["SAFE_PATH", "CommandResult", "run_package_command"]
