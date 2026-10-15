"""A thin, typed wrapper around the container engine CLI.

Every command the isolated runner issues goes through :class:`OciEngine`. The engine CLI is
only ever started with an explicit, minimal environment, so nothing ambient on the operator's
machine (SCM, factory, broker or cloud credentials) can leak into a container command by
omission. Qualification is fail-closed: an engine that cannot enforce the isolated profile is
refused with the missing requirement named, never worked around.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Protocol, runtime_checkable

from enginery.domain.errors import (
    InvalidInputError,
    MissingPrerequisiteError,
    OperationTimeoutError,
)
from enginery.domain.runner import EngineIdentity

RUNNER_LABEL = "dev.enginery.runner"
"""Label (value ``1``) carried by every worker container the runner creates."""

PROBE_LABEL = "dev.enginery.probe"
"""Label (value ``1``) carried by every throw-away probe container."""

OPERATION_LABEL = "dev.enginery.operation"
"""Label carrying the operation id a worker container belongs to."""

LAUNCHER_LABEL = "dev.enginery.launcher"
"""Label carrying the pid, start identity and host of the launcher process that owns a worker."""

ATTEMPT_NETWORK_LABEL = "dev.enginery.attempt-network"
"""Label carrying the operation id an attempt's own internal network was created for."""

ENGINE_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)
"""The only host variables an engine CLI invocation inherits."""

KIND_DOCKER_DESKTOP = "docker-desktop"
KIND_ROOTLESS_DOCKER = "rootless-docker"
KIND_DOCKER = "docker"

_INFO_TIMEOUT_SECONDS = 30.0
_COMMAND_TIMEOUT_SECONDS = 120.0
_COPY_TIMEOUT_SECONDS = 900.0
_STDERR_DETAIL_CHARS = 2000

_LIMIT_SUPPORT: tuple[tuple[str, str], ...] = (
    ("MemoryLimit", "memory limit support"),
    ("SwapLimit", "swap limit support"),
    ("CpuCfsPeriod", "cpu cfs period support"),
    ("CpuCfsQuota", "cpu cfs quota support"),
    ("PidsLimit", "pids limit support"),
)
_LIMIT_WARNING = re.compile(
    r"no (memory limit|swap limit|cpu cfs (quota|period)|pids limit)", re.IGNORECASE
)


class EngineCommandError(MissingPrerequisiteError):
    """The container engine failed or refused a command the runner needs."""


class ObjectNotFoundError(EngineCommandError):
    """A container, image or network the runner asked about does not exist."""


@dataclass(frozen=True, slots=True)
class NetworkInfo:
    """One network as the engine reports it."""

    name: str
    internal: bool
    labels: Mapping[str, str]
    containers: tuple[str, ...]
    """Names of the containers currently attached (running containers only, as the engine lists)."""


class EngineRunner(Protocol):
    """Execute one engine command line and return its outcome without raising on failure."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        input: str | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]: ...


def engine_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The explicit minimal environment an engine CLI invocation runs with."""
    source = os.environ if environ is None else environ
    return {key: source[key] for key in ENGINE_ENVIRONMENT_KEYS if key in source}


def run_subprocess(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    input: str | None = None,  # noqa: A002
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with the minimal engine environment and no inherited stdin.

    Output is decoded as UTF-8 with replacement so a container that logs binary data cannot
    make an engine command raise.
    """
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL if input is None else None,
        input=input,
        timeout=timeout,
        env=engine_environment(),
    )


@runtime_checkable
class CopyRunner(Protocol):
    """Move a tar stream into or out of a container with ``docker cp``.

    ``copy_in`` hands the write end of the stream to ``writer``; ``copy_out`` hands the read
    end to ``reader``. Neither raises on a failed command: the outcome is returned.
    """

    def copy_in(
        self,
        argv: Sequence[str],
        writer: Callable[[IO[bytes]], object],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def copy_out(
        self,
        argv: Sequence[str],
        reader: Callable[[IO[bytes]], object],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


_DRAIN_BYTES = 1024 * 1024


def _stderr_of(errors: IO[bytes]) -> str:
    errors.seek(0)
    return errors.read().decode("utf-8", errors="replace")


class SubprocessCopy:
    """``CopyRunner`` over the docker CLI, run with the minimal engine environment."""

    def copy_in(
        self,
        argv: Sequence[str],
        writer: Callable[[IO[bytes]], object],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                env=engine_environment(),
            )
            try:
                assert process.stdin is not None
                # A broken pipe means the engine stopped reading; its status and stderr say why.
                with contextlib.suppress(BrokenPipeError):
                    writer(process.stdin)
                with contextlib.suppress(BrokenPipeError):
                    process.stdin.close()
                returncode = process.wait(timeout=timeout)
            except BaseException:
                process.kill()
                process.wait()
                raise
            return subprocess.CompletedProcess(tuple(argv), returncode, "", _stderr_of(errors))

    def copy_out(
        self,
        argv: Sequence[str],
        reader: Callable[[IO[bytes]], object],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=engine_environment(),
            )
            try:
                assert process.stdout is not None
                reader(process.stdout)
                # A reader may stop at the end of the archive; let the engine finish writing.
                rest = process.stdout.read(_DRAIN_BYTES)
                if len(rest) == _DRAIN_BYTES:
                    process.kill()  # far more than padding: the engine is not done, stop it
                returncode = process.wait(timeout=timeout)
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                if process.stdout is not None:
                    process.stdout.close()
            return subprocess.CompletedProcess(tuple(argv), returncode, "", _stderr_of(errors))


def _default_runner() -> EngineRunner:
    return run_subprocess


def _tail(text: str) -> str:
    stripped = text.strip()
    return stripped[-_STDERR_DETAIL_CHARS:]


def _is_missing(stderr: str) -> bool:
    """Whether engine error text says the named object does not exist.

    Containers, images and volumes are reported as ``No such ...``, networks as
    ``... not found`` and a path inside a container as ``Could not find the file ...``.
    """
    lowered = stderr.lower().strip()
    return (
        "no such " in lowered
        or "could not find the file" in lowered
        or lowered.endswith("not found")
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _cannot_enforce(missing: Sequence[str], engine: str) -> MissingPrerequisiteError:
    return MissingPrerequisiteError(
        "container engine cannot enforce the isolated runner profile; missing: "
        + "; ".join(missing),
        details={"missing": "; ".join(missing), "engine": engine},
    )


def _option_name(option: str) -> str:
    """``name=seccomp,profile=builtin`` -> ``seccomp``."""
    first = option.split(",", 1)[0]
    return first.removeprefix("name=")


@dataclass(frozen=True, slots=True)
class OciEngine:
    """The docker CLI, optionally pinned to one named context."""

    context: str | None = None
    executable: str = "docker"
    runner: EngineRunner = field(default_factory=_default_runner)
    copier: CopyRunner | None = None

    def run_cli(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        input: str | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        """Run one engine command; a non-zero exit is returned, never raised.

        ``args`` excludes the executable and ``--context``; both are prepended here.
        """
        argv = [self.executable]
        if self.context is not None:
            argv += ["--context", self.context]
        argv += list(args)
        try:
            return self.runner(tuple(argv), timeout=timeout, input=input)
        except subprocess.TimeoutExpired as error:
            raise OperationTimeoutError(
                f"container engine command timed out: {self._summary(args)}",
                details={"timeout_seconds": str(timeout)},
            ) from error
        except OSError as error:
            raise MissingPrerequisiteError(
                f"container engine CLI {self.executable!r} cannot be run: {error}"
            ) from error

    @staticmethod
    def _summary(args: Sequence[str]) -> str:
        # Only the leading words: later arguments may carry environment values.
        return " ".join(args[:2])

    def _checked(
        self, args: Sequence[str], *, timeout: float | None = _COMMAND_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        completed = self.run_cli(args, timeout=timeout)
        if completed.returncode != 0:
            raise self._failure(args, completed)
        return completed

    def _failure(
        self, args: Sequence[str], completed: subprocess.CompletedProcess[str]
    ) -> EngineCommandError:
        stderr = _tail(completed.stderr)
        error_type = ObjectNotFoundError if _is_missing(stderr) else EngineCommandError
        return error_type(
            f"container engine command failed: {self._summary(args)}",
            details={
                "command": self._summary(args),
                "returncode": str(completed.returncode),
                "stderr": stderr,
            },
        )

    # --- identity and qualification -------------------------------------------------

    def _info(self) -> Mapping[str, object]:
        try:
            completed = self.run_cli(
                ("info", "--format", "{{json .}}"), timeout=_INFO_TIMEOUT_SECONDS
            )
        except OperationTimeoutError as error:
            raise MissingPrerequisiteError("container engine did not answer in time") from error
        if completed.returncode != 0:
            raise MissingPrerequisiteError(
                "container engine is unreachable",
                details={
                    "returncode": str(completed.returncode),
                    "stderr": _tail(completed.stderr),
                },
            )
        try:
            info = json.loads(completed.stdout)
        except ValueError as error:
            raise MissingPrerequisiteError("container engine reported unreadable info") from error
        data = _mapping(info)
        if data.get("ServerErrors"):
            raise MissingPrerequisiteError(
                "container engine is unreachable",
                details={"errors": "; ".join(_texts(data.get("ServerErrors")))},
            )
        if not isinstance(data.get("ServerVersion"), str) or not data["ServerVersion"]:
            raise MissingPrerequisiteError("container engine reported no server version")
        return data

    def _identity(self, info: Mapping[str, object]) -> EngineIdentity:
        options = _texts(info.get("SecurityOptions"))
        names = {_option_name(option) for option in options}
        rootless = "rootless" in names
        operating_system = str(info.get("OperatingSystem", ""))
        if operating_system == "Docker Desktop":
            kind = KIND_DOCKER_DESKTOP
        elif rootless:
            kind = KIND_ROOTLESS_DOCKER
        else:
            kind = KIND_DOCKER
        try:
            return EngineIdentity(
                kind=kind,
                server_version=str(info["ServerVersion"]),
                operating_system=operating_system,
                kernel_version=str(info.get("KernelVersion", "")),
                cgroup_version=str(info.get("CgroupVersion", "")),
                security_options=options,
                rootless=rootless,
                context=self.context,
            )
        except InvalidInputError as error:
            raise MissingPrerequisiteError(
                f"container engine did not report a usable identity: {error}"
            ) from error

    def identity(self) -> EngineIdentity:
        """Describe the reachable engine; raises ``MissingPrerequisiteError`` if it is not."""
        return self._identity(self._info())

    def memory_total_bytes(self) -> int:
        """The memory of the machine that runs the engine, in bytes (``docker info`` MemTotal).

        Under Docker Desktop and Lima this is the memory of the engine's virtual machine, which
        holds every container and every tmpfs volume. Raises ``MissingPrerequisiteError`` when
        the engine does not report a usable value: the memory headroom cannot be checked then.
        """
        total = self._info().get("MemTotal")
        if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
            raise MissingPrerequisiteError("container engine reported no usable total memory")
        return total

    def qualify(self) -> EngineIdentity:
        """Return the engine identity only if the engine can enforce the isolated profile.

        Requires cgroup v2, an active seccomp profile, and enforced memory, swap, cpu and pids
        limits. Anything less is refused with every missing requirement named.
        """
        info = self._info()
        options = _texts(info.get("SecurityOptions"))
        cgroup = str(info.get("CgroupVersion", ""))
        missing: list[str] = []
        if cgroup != "2":
            missing.append(f"cgroup v2 (engine reports {cgroup or 'none'!r})")
        seccomp = [option for option in options if _option_name(option) == "seccomp"]
        if not seccomp:
            missing.append("a seccomp profile")
        elif any("profile=unconfined" in option for option in seccomp):
            missing.append("a confined seccomp profile (engine reports unconfined)")
        for key, label in _LIMIT_SUPPORT:
            if info.get(key) is not True:
                missing.append(label)
        for warning in _texts(info.get("Warnings")):
            if _LIMIT_WARNING.search(warning):
                missing.append(f"engine warning: {warning.strip()}")
        # A well-formed identity is part of qualifying: an engine that omits a field fails
        # here, before any receipt that could not be read back is written.
        try:
            identity = self._identity(info)
        except MissingPrerequisiteError:
            if not missing:
                raise
            # The named requirements are the more useful refusal than a malformed identity.
            raise _cannot_enforce(missing, "unknown") from None
        if missing:
            raise _cannot_enforce(missing, identity.kind)
        return identity

    # --- networks -------------------------------------------------------------------
    #
    # No container ever joins a shared worker network. A worker with model access gets an
    # internal network created for its one attempt (``create_attempt_network``) that only the
    # relay is connected to, for the attempt's lifetime; every other container has no network.

    def network_info(self, name: str) -> NetworkInfo | None:
        """What the engine reports about network ``name``; ``None`` when it does not exist."""
        completed = self.run_cli(("network", "inspect", name), timeout=_COMMAND_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            failure = self._failure(("network", "inspect", name), completed)
            if isinstance(failure, ObjectNotFoundError):
                return None
            raise failure
        try:
            networks = json.loads(completed.stdout)
        except ValueError as error:
            raise EngineCommandError("container engine reported unreadable network data") from error
        for network in networks if isinstance(networks, list) else ():
            document = _mapping(network)
            if document.get("Name") != name:
                continue
            labels = _mapping(document.get("Labels"))
            attached = _mapping(document.get("Containers"))
            return NetworkInfo(
                name=name,
                internal=document.get("Internal") is True,
                labels={key: value for key, value in labels.items() if isinstance(value, str)},
                containers=tuple(
                    sorted(
                        entry
                        for entry in (_mapping(item).get("Name") for item in attached.values())
                        if isinstance(entry, str)
                    )
                ),
            )
        return None

    def create_attempt_network(
        self, name: str, *, operation_id: str, launcher: str | None = None
    ) -> NetworkInfo:
        """Create ``name`` as a fresh ``--internal`` network for one attempt of ``operation_id``.

        The name is derived from the operation, so a network left by an earlier attempt of the
        same operation (a launcher that was killed) is removed first and never reused: every
        attempt gets a network no earlier endpoint, address or DNS entry survives in. A same-named
        network that is not an attempt network is refused untouched. ``launcher`` is the label
        that names the creating launcher process, so a later reconcile can tell a live attempt
        from an orphan.
        """
        if self.network_info(name) is not None:
            self.remove_attempt_network(name)
        labels = [f"{RUNNER_LABEL}=1", f"{ATTEMPT_NETWORK_LABEL}={operation_id}"]
        if launcher is not None:
            labels.append(f"{LAUNCHER_LABEL}={launcher}")
        arguments = ["network", "create", "--internal"]
        for label in labels:
            arguments += ["--label", label]
        self._checked((*arguments, name))
        info = self.network_info(name)
        if info is None:
            raise EngineCommandError(f"attempt network {name!r} is absent right after creation")
        if not info.internal:
            raise MissingPrerequisiteError(
                f"attempt network {name!r} is not internal; refusing to attach a worker",
                details={"network": name},
            )
        return info

    def remove_attempt_network(self, name: str) -> None:
        """Disconnect whatever is still attached to attempt network ``name`` and remove it.

        Idempotent: an absent network is not an error. Only a network that carries the attempt
        label is touched; any other network of that name is refused.
        """
        info = self.network_info(name)
        if info is None:
            return
        if ATTEMPT_NETWORK_LABEL not in info.labels:
            raise MissingPrerequisiteError(
                f"a network named {name!r} exists but is not an attempt network; refusing to "
                "remove it",
                details={"network": name},
            )
        for container in info.containers:
            self.disconnect_network(name, container, force=True)
        self.remove_network(name)

    def remove_network(self, name: str) -> None:
        """Remove network ``name`` (no attached container may remain); an absent one is fine."""
        completed = self.run_cli(("network", "rm", name), timeout=_COMMAND_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            failure = self._failure(("network", "rm", name), completed)
            if not isinstance(failure, ObjectNotFoundError):
                raise failure

    def list_attempt_networks(self) -> tuple[str, ...]:
        """Names of every attempt network the runner created (whatever its attempt's state)."""
        completed = self._checked(
            (
                "network",
                "ls",
                "--filter",
                f"label={ATTEMPT_NETWORK_LABEL}",
                "--format",
                "{{.Name}}",
            )
        )
        return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def connect_network(self, network: str, container: str, *, alias: str | None = None) -> None:
        """Attach ``container`` to ``network`` (optionally under a DNS alias)."""
        arguments = ["network", "connect"]
        if alias is not None:
            arguments += ["--alias", alias]
        self._checked((*arguments, network, container))

    def disconnect_network(self, network: str, container: str, *, force: bool = False) -> None:
        """Detach ``container`` from ``network``; one that is not attached (or gone) is fine."""
        arguments = ["network", "disconnect", *(("--force",) if force else ())]
        completed = self.run_cli((*arguments, network, container), timeout=_COMMAND_TIMEOUT_SECONDS)
        if completed.returncode == 0:
            return
        failure = self._failure(arguments, completed)
        stderr = completed.stderr.lower()
        if isinstance(failure, ObjectNotFoundError) or "is not connected" in stderr:
            return
        raise failure

    # --- images and containers ------------------------------------------------------

    def image_inspect(self, reference: str) -> Mapping[str, object]:
        completed = self._checked(("image", "inspect", reference))
        return self._first(completed.stdout, "image")

    def container_inspect(self, name: str) -> Mapping[str, object]:
        completed = self._checked(("container", "inspect", name))
        return self._first(completed.stdout, "container")

    @staticmethod
    def _first(stdout: str, what: str) -> Mapping[str, object]:
        try:
            parsed = json.loads(stdout)
        except ValueError as error:
            raise EngineCommandError(f"container engine reported unreadable {what} data") from error
        if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], Mapping):
            raise EngineCommandError(f"container engine reported no {what} data")
        return parsed[0]

    def container_exists(self, name: str) -> bool:
        try:
            self.container_inspect(name)
        except ObjectNotFoundError:
            return False
        return True

    def start(self, name: str) -> None:
        self._checked(("start", name))

    def wait(self, name: str) -> int:
        """Block until the container stops and return its exit code (``docker wait``)."""
        completed = self._checked(("wait", name), timeout=None)
        try:
            return int(completed.stdout.strip())
        except ValueError as error:
            raise EngineCommandError("container engine reported an unreadable exit code") from error

    def kill(self, name: str, signal: str = "KILL") -> None:
        """Send ``signal``; a container that is already stopped or gone is not an error."""
        completed = self.run_cli(
            ("kill", "--signal", signal, name), timeout=_COMMAND_TIMEOUT_SECONDS
        )
        if completed.returncode == 0:
            return
        stderr = completed.stderr.lower()
        if "no such" in stderr or "is not running" in stderr:
            return
        raise self._failure(("kill", name), completed)

    def remove(self, name: str, *, force: bool = True) -> None:
        """Remove the container; an already absent one is not an error."""
        args = ("rm", "--volumes", *(("--force",) if force else ()), name)
        completed = self.run_cli(args, timeout=_COMMAND_TIMEOUT_SECONDS)
        if completed.returncode == 0:
            return
        stderr = completed.stderr.lower()
        if "no such" in stderr or "already in progress" in stderr:
            return
        raise self._failure(("rm", name), completed)

    def logs_tail(self, name: str, *, lines: int = 200, max_bytes: int = 64 * 1024) -> str:
        """The last ``lines`` log lines, truncated to the final ``max_bytes`` bytes."""
        completed = self.run_cli(
            ("logs", "--tail", str(lines), name), timeout=_COMMAND_TIMEOUT_SECONDS
        )
        if completed.returncode != 0:
            raise self._failure(("logs", name), completed)
        text = completed.stdout + completed.stderr
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return text
        return encoded[-max_bytes:].decode("utf-8", errors="replace")

    def list_runner_containers(self) -> tuple[str, ...]:
        """Names of every container (running or not) the runner created."""
        completed = self._checked(
            ("ps", "--all", "--filter", f"label={RUNNER_LABEL}=1", "--format", "{{.Names}}")
        )
        return tuple(sorted(line.strip() for line in completed.stdout.splitlines() if line.strip()))

    # --- volumes and tar streams ----------------------------------------------------

    def volume_inspect(self, name: str) -> Mapping[str, object]:
        completed = self._checked(("volume", "inspect", name))
        return self._first(completed.stdout, "volume")

    def volume_remove(self, name: str) -> None:
        """Remove one volume; an already absent one is not an error, one in use is."""
        completed = self.run_cli(("volume", "rm", name), timeout=_COMMAND_TIMEOUT_SECONDS)
        if completed.returncode == 0:
            return
        if "no such volume" in completed.stderr.lower():
            return
        raise self._failure(("volume", "rm", name), completed)

    def list_dangling_runner_volumes(self) -> tuple[str, ...]:
        """Names of runner volumes no container refers to any more."""
        completed = self._checked(
            (
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"label={RUNNER_LABEL}=1",
                "--filter",
                "dangling=true",
            )
        )
        return tuple(sorted(line.strip() for line in completed.stdout.splitlines() if line.strip()))

    def _copier(self) -> CopyRunner:
        if self.copier is not None:
            return self.copier
        if isinstance(self.runner, CopyRunner):
            return self.runner
        return SubprocessCopy()

    def _argv(self, args: Sequence[str]) -> tuple[str, ...]:
        argv = [self.executable]
        if self.context is not None:
            argv += ["--context", self.context]
        return (*argv, *args)

    def copy_in(
        self,
        name: str,
        destination: str,
        writer: Callable[[IO[bytes]], object],
        *,
        timeout: float | None = _COPY_TIMEOUT_SECONDS,
    ) -> None:
        """Stream a tar archive built by ``writer`` into ``destination`` of container ``name``.

        The archive's owners are kept (``docker cp -a``). The container must be running for a
        path on a tmpfs volume: such a volume is gone while no container has it mounted.
        """
        args = ("cp", "-a", "-", f"{name}:{destination}")
        self._copy(
            args, lambda argv: self._copier().copy_in(argv, writer, timeout=timeout), timeout
        )

    def copy_out(
        self,
        name: str,
        source: str,
        reader: Callable[[IO[bytes]], object],
        *,
        timeout: float | None = _COPY_TIMEOUT_SECONDS,
    ) -> None:
        """Stream the tar archive of ``source`` in container ``name`` into ``reader``.

        A path that does not exist raises ``ObjectNotFoundError``. A symlink or special file at
        ``source`` is archived as such, never followed.
        """
        args = ("cp", f"{name}:{source}", "-")
        self._copy(
            args, lambda argv: self._copier().copy_out(argv, reader, timeout=timeout), timeout
        )

    def _copy(
        self,
        args: Sequence[str],
        run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
        timeout: float | None,
    ) -> None:
        try:
            completed = run(self._argv(args))
        except subprocess.TimeoutExpired as error:
            raise OperationTimeoutError(
                f"container engine command timed out: {self._summary(args)}",
                details={"timeout_seconds": str(timeout)},
            ) from error
        except OSError as error:
            raise MissingPrerequisiteError(
                f"container engine CLI {self.executable!r} cannot be run: {error}"
            ) from error
        if completed.returncode != 0:
            raise self._failure(args, completed)
