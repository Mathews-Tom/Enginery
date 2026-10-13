"""The supervised host process that runs one isolated worker container.

Run as ``python -m enginery.adapters.oci.launcher --spec <launch spec json> -- <inner command>``.
It qualifies the engine, creates the hardened container, reads back what the engine actually
created and refuses the launch (removing the container) if that is weaker than the profile,
writes the runner receipt, starts the container and waits for it. Whatever happens, the
container is removed before the launcher exits, and the launcher's exit status is the
container's own.

Exit statuses: the container's exit code; ``124`` the launcher's hard deadline passed and the
container was killed; ``125`` the launcher itself faulted or refused the launch; ``143`` and
``130`` the launcher was stopped by SIGTERM or SIGINT.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import verify_image
from enginery.adapters.oci.launch import (
    create_arguments,
    effective_from_inspect,
    launch_violations,
)
from enginery.domain.errors import EngineryError, InvalidInputError
from enginery.domain.runner import (
    ImageIdentity,
    LaunchSpec,
    RunnerReceipt,
    TrustMode,
    profile_violations,
)
from enginery.engine.supervisor import HostIdentity, probe_host
from enginery.ledger.redaction import redact_credential_shaped_text

EXIT_DEADLINE = 124
EXIT_FAULT = 125
DEADLINE_SLACK_SECONDS = 30
"""Time allowed beyond the worker's own budget and stop grace before the launcher kills it."""
LOG_TAIL_BYTES = 64 * 1024

ImageVerifier = Callable[[OciEngine, str], ImageIdentity]
HostProbe = Callable[[], HostIdentity]
Clock = Callable[[], datetime]


class StopSignal:
    """A stop request (SIGTERM or SIGINT) the launcher notices at its next checkpoint."""

    def __init__(self) -> None:
        self.wake = threading.Event()
        self.number: int | None = None

    def request(self, number: int) -> None:
        if self.number is None:
            self.number = number
        self.wake.set()

    @contextlib.contextmanager
    def installed(self) -> Iterator[StopSignal]:
        """Route SIGTERM and SIGINT to this object for the duration of the block."""

        def handler(number: int, _frame: object) -> None:
            self.request(number)

        previous = {
            number: signal.signal(number, handler) for number in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            yield self
        finally:
            for number, original in previous.items():
                signal.signal(number, original)


class _InterruptedError(Exception):
    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number = number


class _RefusedError(Exception):
    """The launch was refused; the message is already on the launcher's stderr."""


class _Log:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def __call__(self, message: str) -> None:
        self._stream.write(f"launcher: {message}\n")
        self._stream.flush()

    def raw(self, text: str) -> None:
        self._stream.write(text)
        if not text.endswith("\n"):
            self._stream.write("\n")
        self._stream.flush()


# --- host paths ----------------------------------------------------------------------


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _require_directory(path: str, role: str) -> str:
    """The real directory at ``path``; a symlink, file or absent path is refused."""
    try:
        info = os.lstat(path)
    except OSError as error:
        raise InvalidInputError(f"{role} directory is not accessible: {path}") from error
    if stat.S_ISLNK(info.st_mode):
        raise InvalidInputError(f"{role} directory must not be a symlink: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidInputError(f"{role} path is not a directory: {path}")
    return os.path.realpath(path)


def admit_paths(spec: LaunchSpec) -> None:
    """Refuse host paths a worker must not be given.

    The workspace and result directories must be real, distinct, non-nested directories, and
    the receipt must live outside both so the worker can never read or forge it.
    """
    workspace = _require_directory(spec.workspace_path, "workspace")
    out = _require_directory(spec.out_path, "result")
    if _within(workspace, out) or _within(out, workspace):
        raise InvalidInputError("the workspace and result directories must not overlap")
    receipt = Path(spec.receipt_path)
    receipt_real = str(Path(os.path.realpath(receipt.parent)) / receipt.name)
    for role, root in (("workspace", workspace), ("result", out)):
        if _within(receipt_real, root) or _within(root, receipt_real):
            raise InvalidInputError(f"the receipt path must be outside the {role} directory")


def _open_up(path: str) -> None:
    """``chmod a+rwX`` on one regular file or directory; symlinks and specials are skipped."""
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode):
        wanted = 0o777
    elif stat.S_ISREG(info.st_mode):
        wanted = 0o666 | (0o111 if info.st_mode & 0o111 else 0)
    else:
        return
    current = stat.S_IMODE(info.st_mode)
    if current & wanted != wanted:
        os.chmod(path, current | wanted)


def _raise(error: OSError) -> None:
    raise error


def make_tree_writable(root: str) -> None:
    """``chmod -R a+rwX`` without ever following a symlink.

    The container's non-root user must be able to write the bind-mounted directories whatever
    uid the engine maps it to; the worker then creates everything with umask 0, so the host
    can always delete it again.
    """
    _require_directory(root, "tree")
    for directory, _subdirectories, files in os.walk(root, followlinks=False, onerror=_raise):
        _open_up(directory)
        for name in files:
            _open_up(os.path.join(directory, name))


def write_receipt(path: str, receipt: RunnerReceipt) -> None:
    """Atomically write the receipt as a 0600 file; the parent directory is created 0700."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt.to_state(), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --- the run -------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _checkpoint(stop: StopSignal) -> None:
    if stop.number is not None:
        raise _InterruptedError(stop.number)


def _await_exit(
    engine: OciEngine, name: str, *, stop: StopSignal, deadline_seconds: float
) -> int | None:
    """Wait for the container to stop; ``None`` means the hard deadline passed first.

    A stop request raises ``_InterruptedError``.
    """
    outcome: dict[str, object] = {}
    finished = threading.Event()

    def waiter() -> None:
        try:
            outcome["code"] = engine.wait(name)
        except BaseException as error:
            outcome["error"] = error
        finally:
            finished.set()
            stop.wake.set()

    thread = threading.Thread(target=waiter, name="oci-wait", daemon=True)
    thread.start()
    deadline = time.monotonic() + deadline_seconds
    while True:
        _checkpoint(stop)
        if finished.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        stop.wake.wait(remaining)
    thread.join(timeout=5)
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    code = outcome.get("code")
    if not isinstance(code, int):
        raise _RefusedError("the engine reported no exit code for the container")
    return code


def run_launcher(
    spec: LaunchSpec,
    inner_command: Sequence[str],
    *,
    engine: OciEngine,
    verify: ImageVerifier = verify_image,
    host_probe: HostProbe = probe_host,
    clock: Clock = _utc_now,
    stderr: TextIO | None = None,
    stop: StopSignal | None = None,
    deadline_slack_seconds: int = DEADLINE_SLACK_SECONDS,
) -> int:
    """Run one isolated worker attempt and return the launcher's exit status."""
    log = _Log(sys.stderr if stderr is None else stderr)
    stop = StopSignal() if stop is None else stop
    name = spec.container_name
    created = False
    started = False
    code = EXIT_FAULT
    try:
        _checkpoint(stop)
        admit_paths(spec)
        # A receipt left by an earlier attempt describes a container that no longer exists;
        # none must survive to be mistaken for this attempt's.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(spec.receipt_path)
        arguments = create_arguments(spec, inner_command)
        identity = engine.qualify()
        image = verify(engine, spec.image)
        if image.image_id != spec.image:
            raise _RefusedError("the verified image is not the pinned image")
        engine.ensure_internal_network(spec.profile.network)
        _checkpoint(stop)
        if engine.container_exists(name):
            log(f"removing a container left by an earlier attempt of this operation: {name}")
            engine.remove(name)
        make_tree_writable(spec.workspace_path)
        make_tree_writable(spec.out_path)

        created = True  # the engine can hold a container even when create reports failure
        completed = engine.run_cli(arguments)
        if completed.returncode != 0:
            raise _RefusedError(
                "the engine could not create the container: "
                + redact_credential_shaped_text(completed.stderr.strip()[-2000:])
            )
        if completed.stderr.strip():
            raise _RefusedError(
                "the engine warned while creating the container, so its limits may not hold: "
                + redact_credential_shaped_text(completed.stderr.strip()[-2000:])
            )
        _checkpoint(stop)

        container = engine.container_inspect(name)
        effective = effective_from_inspect(container, engine.image_inspect(spec.image))
        violations = (
            *profile_violations(spec.profile, effective),
            *launch_violations(spec, container),
        )
        if violations:
            for violation in violations:
                log(f"profile violation: {violation}")
            raise _RefusedError("the created container is weaker than the isolated profile")

        host = host_probe()
        receipt = RunnerReceipt(
            operation_id=spec.operation_id,
            trust_mode=TrustMode.ISOLATED_OCI,
            profile_name=spec.profile.name,
            profile_digest=spec.profile.digest,
            engine=identity,
            image=image,
            container_name=name,
            host={
                "machine_id": host.machine_id,
                "boot_id": host.boot_id,
                "hostname": host.hostname,
            },
            effective=effective,
            permissions=spec.permissions,
            harness_version=None,
            harness_protocol=None,
            started_at=clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        write_receipt(spec.receipt_path, receipt)
        _checkpoint(stop)

        engine.start(name)
        started = True
        deadline = (
            spec.time_budget_seconds
            + spec.profile.resources.stop_grace_seconds
            + deadline_slack_seconds
        )
        exited = _await_exit(engine, name, stop=stop, deadline_seconds=deadline)
        if exited is None:
            log(f"hard deadline of {deadline}s passed; killing the container")
            engine.kill(name)
            code = EXIT_DEADLINE
        else:
            code = exited
    except _InterruptedError as interrupt:
        log(f"stopped by signal {interrupt.number}; killing the container")
        if created:
            with contextlib.suppress(EngineryError):
                engine.kill(name)
        code = 128 + interrupt.number
    except _RefusedError as refusal:
        log(str(refusal))
        code = EXIT_FAULT
    except (EngineryError, OSError) as error:
        log(_describe(error))
        code = EXIT_FAULT
    finally:
        if created:
            _tail_and_remove(engine, name, log, show_logs=started and code != 0)
    return code


def _describe(error: BaseException) -> str:
    """One diagnostic line for an error, including the engine details it carries."""
    text = f"{type(error).__name__}: {error}"
    details = getattr(error, "details", None)
    if details:
        text += " (" + "; ".join(f"{key}={value}" for key, value in details.items()) + ")"
    return redact_credential_shaped_text(text)


def _tail_and_remove(engine: OciEngine, name: str, log: _Log, *, show_logs: bool) -> None:
    if show_logs:
        try:
            tail = engine.logs_tail(name, max_bytes=LOG_TAIL_BYTES)
        except EngineryError as error:
            log(f"container output is unavailable: {_describe(error)}")
        else:
            if tail.strip():
                log(f"container output (last {LOG_TAIL_BYTES} bytes at most):")
                log.raw(redact_credential_shaped_text(tail))
    try:
        engine.remove(name, force=True)
    except EngineryError as error:
        log(f"the container could not be removed: {_describe(error)}")


def _parse(arguments: Sequence[str]) -> tuple[LaunchSpec, tuple[str, ...]]:
    items = list(arguments)
    if "--" not in items:
        raise InvalidInputError("usage: launcher --spec <json> -- <command> [args...]")
    split = items.index("--")
    wrapper, inner = items[:split], tuple(items[split + 1 :])
    if len(wrapper) != 2 or wrapper[0] != "--spec":
        raise InvalidInputError("usage: launcher --spec <json> -- <command> [args...]")
    if not inner:
        raise InvalidInputError("the launcher needs a command after '--'")
    try:
        state = json.loads(wrapper[1])
    except ValueError as error:
        raise InvalidInputError("the launch spec is not valid JSON") from error
    return LaunchSpec.from_state(state), inner


def main(
    arguments: Sequence[str] | None = None,
    *,
    engine: OciEngine | None = None,
    verify: ImageVerifier = verify_image,
    host_probe: HostProbe = probe_host,
) -> int:
    """Command-line entry point; returns the exit status.

    ``engine``, ``verify`` and ``host_probe`` exist so tests can substitute a fake engine,
    image verifier and host identity; the command line never supplies them.
    """
    try:
        spec, inner = _parse(sys.argv[1:] if arguments is None else arguments)
    except InvalidInputError as error:
        sys.stderr.write(f"launcher: {error}\n")
        return EXIT_FAULT
    chosen = OciEngine(context=spec.docker_context) if engine is None else engine
    with StopSignal().installed() as stop:
        return run_launcher(
            spec, inner, engine=chosen, verify=verify, host_probe=host_probe, stop=stop
        )


if __name__ == "__main__":
    raise SystemExit(main())
