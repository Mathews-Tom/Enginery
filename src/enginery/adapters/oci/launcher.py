"""The supervised host process that runs one isolated worker container.

Run as ``python -m enginery.adapters.oci.launcher --spec <launch spec json> -- <inner command>``.
It qualifies the engine, creates the hardened container, reads back what the engine actually
created and refuses the launch (removing the container) if that is weaker than the profile,
writes the runner receipt, starts the container and waits for it. While the worker runs it
measures the workspace and result trees against their disk bounds and kills the container when
one is exceeded. Whatever happens, the container is removed before the launcher exits.

The launcher records how the attempt ended in its own host-only outcome file (beside the
receipt, outside every worker mount). Collection classifies the attempt from that file; the
launcher's process exit status is a courtesy for the supervisor and proves nothing, because a
contained command can exit with any status. Exit statuses: the container's exit code; ``124`` the
launcher's hard deadline passed and the container was killed; ``125`` the launcher itself
faulted, refused the launch or killed a worker that outgrew its disk bound; ``143`` and ``130``
the launcher was stopped by SIGTERM or SIGINT.

``python -m enginery.adapters.oci.launcher --reconcile [--docker-context NAME]`` removes runner
containers whose launcher process is gone.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from enginery.adapters.oci.diskbound import DiskBound
from enginery.adapters.oci.engine import KIND_DOCKER_DESKTOP, OciEngine
from enginery.adapters.oci.image import verify_image
from enginery.adapters.oci.launch import (
    BUNDLE_FILE_NAME,
    DISK_POLL_SECONDS,
    create_arguments,
    effective_from_inspect,
    launch_violations,
    resource_bounds,
)
from enginery.adapters.oci.reconcile import ReconcileReport, reconcile_runner_containers
from enginery.domain.errors import EngineryError, InvalidInputError
from enginery.domain.runner import (
    EngineIdentity,
    ImageIdentity,
    LaunchOutcome,
    LaunchSpec,
    OutcomeState,
    PermissionMapping,
    RunnerReceipt,
    TrustMode,
    outcome_path_for,
    profile_violations,
)
from enginery.engine.supervisor import HostIdentity, ProcessIdentity, probe_host, probe_process
from enginery.ledger.redaction import redact_credential_shaped_text

EXIT_DEADLINE = 124
EXIT_FAULT = 125
DEADLINE_SLACK_SECONDS = 30
"""Time allowed beyond the worker's own budget and stop grace before the launcher kills it."""
LOG_TAIL_BYTES = 64 * 1024
_PRIVATE_ROOT_FORBIDDEN_BITS = 0o077
_NONCE_BYTES = 16

ImageVerifier = Callable[[OciEngine, str], ImageIdentity]
HostProbe = Callable[[], HostIdentity]
Clock = Callable[[], datetime]
Identify = Callable[[], ProcessIdentity]


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

    def __init__(self, message: str, violations: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.violations = tuple(violations)


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


def _require_private_root(root: str) -> None:
    """Refuse a directory that anyone but the operator can enter.

    The workspace and result trees are opened up for the container's uid (on engines that map
    it to an unrelated host uid), so they are only as private as the directory that holds them:
    that directory must be a real directory owned by the operator with no group or other access.
    """
    try:
        info = os.lstat(root)
    except OSError as error:
        raise InvalidInputError(
            f"the directory holding the worker trees is unreadable: {root}"
        ) from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise InvalidInputError(
            f"the directory holding the worker trees is not a directory: {root}"
        )
    if info.st_uid != os.geteuid():
        raise InvalidInputError(
            f"the directory holding the worker trees is not owned by the operator: {root}"
        )
    if stat.S_IMODE(info.st_mode) & _PRIVATE_ROOT_FORBIDDEN_BITS:
        raise InvalidInputError(
            "the directory holding the worker trees must be mode 0700 "
            f"(no group or other access): {root}"
        )


def admit_paths(spec: LaunchSpec) -> None:
    """Refuse host paths a worker must not be given.

    The workspace and result directories must be real, distinct, non-nested directories that
    both live under one private (operator-owned, mode 0700) directory, and the receipt must
    live outside both so the worker can never read or forge it.
    """
    workspace = _require_directory(spec.workspace_path, "workspace")
    out = _require_directory(spec.out_path, "result")
    if _within(workspace, out) or _within(out, workspace):
        raise InvalidInputError("the workspace and result directories must not overlap")
    root = os.path.dirname(workspace)
    _require_private_root(root)
    if not _within(out, root):
        raise InvalidInputError(
            "the result directory must live under the directory that holds the workspace"
        )
    receipt = Path(spec.receipt_path)
    receipt_real = str(Path(os.path.realpath(receipt.parent)) / receipt.name)
    for role, tree in (("workspace", workspace), ("result", out)):
        if _within(receipt_real, tree) or _within(tree, receipt_real):
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


def _unlink_file(path: str) -> None:
    """Unlink ``path`` unless it is absent or a directory; a symlink is removed itself."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        pass


def _raise(error: OSError) -> None:
    raise error


def make_tree_writable(root: str) -> None:
    """``chmod -R a+rwX`` without ever following a symlink.

    Needed only where the engine maps the container's non-root uid to a uid that does not own
    the tree (see ``needs_open_permissions``); the worker then creates everything with umask 0,
    so the host can always delete it again.
    """
    _require_directory(root, "tree")
    for directory, _subdirectories, files in os.walk(root, followlinks=False, onerror=_raise):
        _open_up(directory)
        for name in files:
            _open_up(os.path.join(directory, name))


def needs_open_permissions(engine: EngineIdentity) -> bool:
    """Whether the engine's uid mapping requires the trees to be opened to other uids.

    Docker Desktop runs the containers behind a file-sharing layer that presents a bind mount
    to the container user with the operator's own authority, so a 0700 tree owned by the
    operator is writable by the worker as it stands and nothing is opened. On every other
    engine the container's uid is a different host uid (or an unprivileged sub-uid), so the
    trees must be world read-write; they sit inside the private root ``admit_paths`` demands.
    """
    return engine.kind != KIND_DOCKER_DESKTOP


def _discard(path: str) -> None:
    """Remove ``path`` if it exists; a symlink is removed itself, never followed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _write_private_json(path: str, state: dict[str, object]) -> None:
    """Atomically write ``state`` as a 0600 file; the parent directory is created 0700."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def write_receipt(path: str, receipt: RunnerReceipt) -> None:
    """Atomically write the receipt as a 0600 file; the parent directory is created 0700."""
    _write_private_json(path, receipt.to_state())


def write_outcome(path: str, outcome: LaunchOutcome) -> None:
    """Atomically write the launcher's outcome as a 0600 file beside the receipt."""
    _write_private_json(path, outcome.to_state())


# --- the run -------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _own_identity() -> ProcessIdentity:
    identity = probe_process(os.getpid())
    if identity is None:  # the launcher is running, so its own pid must exist
        raise _RefusedError("the launcher cannot establish its own process identity")
    return identity


def _checkpoint(stop: StopSignal) -> None:
    if stop.number is not None:
        raise _InterruptedError(stop.number)


def _contains_run(command: Sequence[str], run: Sequence[str]) -> bool:
    """Whether ``run`` appears in ``command`` as one contiguous run of arguments."""
    if not run:
        return True
    width = len(run)
    return any(
        tuple(command[start : start + width]) == tuple(run)
        for start in range(len(command) - width + 1)
    )


def _require_permission_mapping(permissions: PermissionMapping, inner: Sequence[str]) -> None:
    if permissions.unconditional_bypass:
        raise _RefusedError(
            f"the permission mapping {permissions.name!r} turns the harness's permission checks "
            "off; an isolated launch never carries a bypass"
        )
    if not _contains_run(inner, permissions.arguments):
        raise _RefusedError(
            f"the inner command does not carry the arguments of permission mapping "
            f"{permissions.name!r}, so the harness would not run under the recorded permissions"
        )


@dataclass(frozen=True, slots=True)
class _Waited:
    """How waiting for the container ended: its exit code, the deadline, or a disk bound."""

    code: int | None = None
    deadline: bool = False
    bound: str | None = None


def _await_exit(
    engine: OciEngine,
    name: str,
    *,
    stop: StopSignal,
    deadline_seconds: float,
    bound: DiskBound | None = None,
    poll_seconds: float = DISK_POLL_SECONDS,
) -> _Waited:
    """Wait for the container to stop, measuring its disk bounds while it runs.

    A stop request raises ``_InterruptedError``. The measurement is repeated no more often than
    ``poll_seconds`` and backs off to three times its own duration, so a large tree cannot make
    the launcher spend its time measuring.
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
    next_measurement = time.monotonic() + poll_seconds
    while True:
        _checkpoint(stop)
        if finished.is_set():
            break
        now = time.monotonic()
        if deadline - now <= 0:
            return _Waited(deadline=True)
        if bound is not None and now >= next_measurement:
            violation = bound.violation()
            if violation is not None:
                return _Waited(bound=violation)
            next_measurement = time.monotonic() + max(poll_seconds, 3 * (time.monotonic() - now))
            continue
        horizon = deadline if bound is None else min(deadline, next_measurement)
        stop.wake.wait(max(horizon - now, 0.0))
    thread.join(timeout=5)
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    code = outcome.get("code")
    if not isinstance(code, int):
        raise _RefusedError("the engine reported no exit code for the container")
    if bound is not None:
        # A writer that outran every poll (a burst before the first measurement, then exit)
        # still leaves an over-cap tree behind: measure once more after the container stopped.
        violation = bound.violation()
        if violation is not None:
            return _Waited(bound=violation)
    return _Waited(code=code)


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
    identify: Identify = _own_identity,
    poll_seconds: float = DISK_POLL_SECONDS,
) -> int:
    """Run one isolated worker attempt and return the launcher's exit status."""
    log = _Log(sys.stderr if stderr is None else stderr)
    stop = StopSignal() if stop is None else stop
    name = spec.container_name
    nonce = secrets.token_hex(_NONCE_BYTES)
    outcome_path = outcome_path_for(spec.receipt_path)
    created = False
    started = False
    recording = False
    code = EXIT_FAULT
    state = OutcomeState.FAULTED
    exit_code: int | None = None
    detail = ""
    violations: tuple[str, ...] = ()

    def record(
        new_state: OutcomeState,
        *,
        exited: int | None = None,
        reason: str = "",
        problems: Sequence[str] = (),
    ) -> None:
        write_outcome(
            outcome_path,
            LaunchOutcome(
                operation_id=spec.operation_id,
                nonce=nonce,
                state=new_state,
                exit_code=exited,
                detail=redact_credential_shaped_text(reason)[-2000:],
                violations=tuple(problems),
            ),
        )

    try:
        _checkpoint(stop)
        # A receipt or outcome left by an earlier attempt describes a container that no longer
        # exists; none must survive to be mistaken for this attempt's. A directory is left for
        # admission to refuse.
        for stale in (spec.receipt_path, outcome_path):
            _unlink_file(stale)
        admit_paths(spec)
        record(OutcomeState.LAUNCHING)
        recording = True
        _require_permission_mapping(spec.permissions, inner_command)
        launcher = identify()
        arguments = create_arguments(spec, inner_command, nonce=nonce, launcher=launcher)
        identity = engine.qualify()
        image = verify(engine, spec.image)
        if image.image_id != spec.image:
            raise _RefusedError("the verified image is not the pinned image")
        engine.ensure_internal_network(spec.profile.network)
        _checkpoint(stop)
        if engine.container_exists(name):
            log(f"removing a container left by an earlier attempt of this operation: {name}")
            engine.remove(name)
        # Nothing an earlier attempt left in the result directory may be read as this one's.
        _discard(os.path.join(spec.out_path, spec.result_name))
        _discard(os.path.join(spec.out_path, BUNDLE_FILE_NAME))
        if needs_open_permissions(identity):
            make_tree_writable(spec.workspace_path)
            make_tree_writable(spec.out_path)
        try:
            reconciled = reconcile_runner_containers(engine, keep=frozenset({name}))
        except EngineryError as error:
            log(f"orphaned containers could not be reconciled: {_describe(error)}")
        else:
            for orphan in reconciled.removed:
                log(f"removed an orphaned container whose launcher is gone: {orphan}")
            for orphan, reason in reconciled.failed:
                log(f"orphaned container {orphan} could not be reconciled: {reason}")

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
        found = (
            *profile_violations(spec.profile, effective),
            *launch_violations(spec, container),
        )
        if found:
            for violation in found:
                log(f"profile violation: {violation}")
            raise _RefusedError("the created container is weaker than the isolated profile", found)

        bounds = resource_bounds(spec.profile)
        bound = DiskBound(workspace=spec.workspace_path, out=spec.out_path, bounds=bounds)
        over = bound.violation()
        if over is not None:
            raise _RefusedError(f"the worker trees are already over their bound: {over}")

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
            harness_version=spec.harness_version,
            harness_protocol=spec.harness_protocol,
            started_at=clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            bounds=bounds,
        )
        write_receipt(spec.receipt_path, receipt)
        _checkpoint(stop)

        engine.start(name)
        started = True
        record(OutcomeState.RUNNING)
        deadline = (
            spec.time_budget_seconds
            + spec.profile.resources.stop_grace_seconds
            + deadline_slack_seconds
        )
        waited = _await_exit(
            engine,
            name,
            stop=stop,
            deadline_seconds=deadline,
            bound=bound,
            poll_seconds=poll_seconds,
        )
        if waited.bound is not None:
            log(f"{waited.bound}; killing the container")
            engine.kill(name)
            state, detail, violations = OutcomeState.BOUND_EXCEEDED, waited.bound, (waited.bound,)
            code = EXIT_FAULT
        elif waited.deadline:
            log(f"hard deadline of {deadline}s passed; killing the container")
            engine.kill(name)
            state, detail = OutcomeState.DEADLINE, f"hard deadline of {deadline}s passed"
            code = EXIT_DEADLINE
        else:
            assert waited.code is not None  # _await_exit raises unless it has a code
            state, exit_code = OutcomeState.EXITED, waited.code
            code = waited.code
    except _InterruptedError as interrupt:
        log(f"stopped by signal {interrupt.number}; killing the container")
        if created:
            with contextlib.suppress(EngineryError):
                engine.kill(name)
        state, detail = OutcomeState.INTERRUPTED, f"stopped by signal {interrupt.number}"
        code = 128 + interrupt.number
    except _RefusedError as refusal:
        log(str(refusal))
        state, detail, violations = OutcomeState.REFUSED, str(refusal), refusal.violations
        code = EXIT_FAULT
    except (EngineryError, OSError) as error:
        detail = _describe(error)
        log(detail)
        state = OutcomeState.FAULTED
        code = EXIT_FAULT
    finally:
        if created:
            _tail_and_remove(engine, name, log, show_logs=started and code != 0)
        if recording:
            # If this write fails the outcome stays "launching"/"running" and collection treats
            # the attempt as unfinished, which is a failure, never an acceptance.
            with contextlib.suppress(OSError):
                record(state, exited=exit_code, reason=detail, problems=violations)
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


def _print_reconcile(report: ReconcileReport, stream: TextIO) -> None:
    for name in report.removed:
        stream.write(f"removed orphaned runner container {name}\n")
    for name in report.kept:
        stream.write(f"kept runner container {name} (its launcher is alive)\n")
    for name in report.unattributed:
        stream.write(f"left runner container {name} (no launcher on this host is named)\n")
    for name, reason in report.failed:
        stream.write(f"could not reconcile runner container {name}: {reason}\n")


def reconcile_main(
    arguments: Sequence[str], *, engine: OciEngine | None = None, stdout: TextIO | None = None
) -> int:
    """``--reconcile [--docker-context NAME]``: remove runner containers whose launcher is gone."""
    items = list(arguments)
    context: str | None = None
    if items == ["--reconcile"]:
        pass
    elif len(items) == 3 and items[0] == "--reconcile" and items[1] == "--docker-context":
        context = items[2]
    else:
        sys.stderr.write("usage: launcher --reconcile [--docker-context NAME]\n")
        return EXIT_FAULT
    chosen = OciEngine(context=context) if engine is None else engine
    try:
        report = reconcile_runner_containers(chosen)
    except EngineryError as error:
        sys.stderr.write(f"launcher: {_describe(error)}\n")
        return EXIT_FAULT
    _print_reconcile(report, sys.stdout if stdout is None else stdout)
    return EXIT_FAULT if report.failed else 0


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
    given = sys.argv[1:] if arguments is None else list(arguments)
    if given and given[0] == "--reconcile":
        return reconcile_main(given, engine=engine)
    try:
        spec, inner = _parse(given)
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
