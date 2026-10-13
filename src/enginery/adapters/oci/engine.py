"""A thin, typed wrapper around the container engine CLI.

Every command the isolated runner issues goes through :class:`OciEngine`. The engine CLI is
only ever started with an explicit, minimal environment, so nothing ambient on the operator's
machine (SCM, factory, broker or cloud credentials) can leak into a container command by
omission. Qualification is fail-closed: an engine that cannot enforce the isolated profile is
refused with the missing requirement named, never worked around.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from enginery.domain.errors import MissingPrerequisiteError, OperationTimeoutError
from enginery.domain.runner import EngineIdentity

RUNNER_LABEL = "dev.enginery.runner"
"""Label (value ``1``) carried by every worker container the runner creates."""

PROBE_LABEL = "dev.enginery.probe"
"""Label (value ``1``) carried by every throw-away probe container."""

OPERATION_LABEL = "dev.enginery.operation"
"""Label carrying the operation id a worker container belongs to."""

LAUNCHER_LABEL = "dev.enginery.launcher"
"""Label carrying the pid, start identity and host of the launcher process that owns a worker."""

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


def _default_runner() -> EngineRunner:
    return run_subprocess


def _tail(text: str) -> str:
    stripped = text.strip()
    return stripped[-_STDERR_DETAIL_CHARS:]


def _is_missing(stderr: str) -> bool:
    """Whether engine error text says the named object does not exist.

    Containers and images are reported as ``No such ...``, networks as ``... not found``.
    """
    lowered = stderr.lower().strip()
    return "no such " in lowered or lowered.endswith("not found")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


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

    def identity(self) -> EngineIdentity:
        """Describe the reachable engine; raises ``MissingPrerequisiteError`` if it is not."""
        return self._identity(self._info())

    def qualify(self) -> EngineIdentity:
        """Return the engine identity only if the engine can enforce the isolated profile.

        Requires cgroup v2, an active seccomp profile, and enforced memory, swap, cpu and pids
        limits. Anything less is refused with every missing requirement named.
        """
        info = self._info()
        identity = self._identity(info)
        missing: list[str] = []
        if identity.cgroup_version != "2":
            missing.append(f"cgroup v2 (engine reports {identity.cgroup_version or 'none'!r})")
        seccomp = [
            option for option in identity.security_options if _option_name(option) == "seccomp"
        ]
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
        if missing:
            raise MissingPrerequisiteError(
                "container engine cannot enforce the isolated runner profile; missing: "
                + "; ".join(missing),
                details={"missing": "; ".join(missing), "engine": identity.kind},
            )
        return identity

    # --- networks -------------------------------------------------------------------

    def _network_internal(self, name: str) -> bool | None:
        """True/False when the network exists, None when it does not."""
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
            if _mapping(network).get("Name") == name:
                return _mapping(network).get("Internal") is True
        return None

    def ensure_internal_network(self, name: str) -> None:
        """Create ``name`` as an ``--internal`` network, or verify that it already is one.

        Idempotent and tolerant of a concurrent creator. A same-named network that is not
        internal is refused: it would give the worker a route out.
        """
        state = self._network_internal(name)
        if state is None:
            created = self.run_cli(
                ("network", "create", "--internal", "--label", f"{RUNNER_LABEL}=1", name),
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
            if created.returncode != 0:
                state = self._network_internal(name)
                if state is None:
                    raise self._failure(("network", "create"), created)
            else:
                state = self._network_internal(name)
        if state is None:
            raise EngineCommandError(f"runner network {name!r} is absent right after creation")
        if not state:
            raise MissingPrerequisiteError(
                f"runner network {name!r} exists but is not internal; refusing to attach a worker",
                details={"network": name},
            )

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
