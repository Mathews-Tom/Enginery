"""Run and inspect the operator-owned model-access relay container.

The relay is the only container that holds the model gateway bearer. It is pinned to an exact
image, hardened like a worker (non-root, no capabilities, no-new-privileges, read-only root
filesystem, bounded resources), and dual-homed: on the profile's internal network under the
alias ``enginery-relay``, where workers reach it, and on a bridge network that gives it a route
to the gateway. A worker is attached to the internal network only, so the relay is the sole
path to a model.

The token file is mounted read-only and is only ever stat-ed on the host: its content is read by
the relay process alone, on every request, and is never read, logged or recorded here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from enginery.adapters.oci.engine import (
    KIND_DOCKER,
    KIND_ROOTLESS_DOCKER,
    RUNNER_LABEL,
    EngineCommandError,
    ObjectNotFoundError,
    OciEngine,
)
from enginery.adapters.oci.image import verify_relay_image
from enginery.adapters.oci.launch import effective_from_inspect, hardening_arguments
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.runner import (
    RELAY_HOSTNAME,
    RELAY_PORT,
    EffectiveContainer,
    ResourceLimits,
    RunnerProfile,
)

RELAY_CONTAINER_NAME = RELAY_HOSTNAME
RELAY_LABEL = "dev.enginery.relay"
"""Label (value ``1``) carried by the relay container."""

RELAY_SPEC_LABEL = "dev.enginery.relay-spec"
"""Label carrying the digest of everything the relay was created from."""

EGRESS_NETWORK = "enginery-runner-egress"
"""The bridge network that gives the relay (never a worker) a route to the gateway."""

RELAY_TOKEN_MOUNT = "/run/relay/gateway-token"
"""Where the token file appears inside the relay container (read-only)."""

RELAY_LIMITS = ResourceLimits(
    memory_mib=256, cpus=1.0, pids=64, tmp_mib=16, home_mib=1, stop_grace_seconds=5
)
"""The relay forwards HTTP and nothing else; it needs far less than a worker."""

HOST_GATEWAY_NAME = "host.docker.internal"

ROOTLESS_RELAY_USER = "0:0"
"""On a rootless engine this is the invoking user, the only one that can read a 0600 token file."""

_UP_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.5
_COMMAND_TIMEOUT_SECONDS = 120.0
_HEALTH_TIMEOUT_SECONDS = 20.0
_MAX_TOKEN_FILE_BYTES = 4096
_HEALTH_PROGRAM = (
    "import sys, urllib.error, urllib.request\n"
    "try:\n"
    f"    urllib.request.urlopen('http://127.0.0.1:{RELAY_PORT}/healthz', timeout=5)\n"
    "except urllib.error.HTTPError as error:\n"
    "    sys.stderr.write(error.read().decode('utf-8', 'replace')[:300])\n"
    "    sys.exit(1)\n"
    "except Exception as error:\n"
    "    sys.stderr.write(type(error).__name__)\n"
    "    sys.exit(1)\n"
)


@dataclass(frozen=True, slots=True)
class RelayStatus:
    """What the engine reports about the relay, and whether it can serve a worker now."""

    present: bool
    running: bool
    healthy: bool
    image_id: str | None
    networks: tuple[str, ...]
    upstream: str | None
    detail: str

    def to_state(self) -> dict[str, object]:
        return {
            "present": self.present,
            "running": self.running,
            "healthy": self.healthy,
            "image_id": self.image_id,
            "networks": list(self.networks),
            "upstream": self.upstream,
            "detail": self.detail,
        }


def validate_upstream(upstream: str) -> tuple[str, str, int]:
    """Check ``http(s)://host[:port]`` and return ``(scheme, host, port)``."""
    parts = urllib.parse.urlsplit(upstream)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidInputError("relay upstream must be http(s)://host[:port]")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise InvalidInputError("relay upstream must carry no path, query or credentials")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise InvalidInputError("relay upstream port is invalid") from error
    return parts.scheme, parts.hostname, port


def check_token_file(path: Path) -> Path:
    """Refuse a gateway token file that is not a private regular file owned by this user.

    Only the file's metadata is inspected; its content is never opened.
    """
    candidate = Path(path)
    text = str(candidate)
    if not candidate.is_absolute():
        raise InvalidInputError("the gateway token file path must be absolute")
    if ":" in text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise InvalidInputError(
            "the gateway token file path cannot be bind-mounted: it contains ':' or a control "
            "character"
        )
    try:
        info = os.lstat(candidate)
    except OSError as error:
        raise MissingPrerequisiteError(
            f"the gateway token file {candidate} cannot be inspected: {error.strerror}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise MissingPrerequisiteError(
            f"the gateway token file {candidate} is a symbolic link; name the file itself"
        )
    if not stat.S_ISREG(info.st_mode):
        raise MissingPrerequisiteError(f"the gateway token file {candidate} is not a regular file")
    if info.st_uid != os.geteuid():
        raise MissingPrerequisiteError(
            f"the gateway token file {candidate} is not owned by the invoking user"
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise MissingPrerequisiteError(
            f"the gateway token file {candidate} must have mode 0600 "
            f"(found {stat.S_IMODE(info.st_mode):04o})"
        )
    if info.st_size == 0 or info.st_size > _MAX_TOKEN_FILE_BYTES:
        raise MissingPrerequisiteError(
            f"the gateway token file {candidate} must hold a single token "
            f"(1 to {_MAX_TOKEN_FILE_BYTES} bytes)"
        )
    return candidate


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def relay_violations(
    effective: EffectiveContainer, *, expected_user: str, limits: ResourceLimits
) -> tuple[str, ...]:
    """Every way the relay container the engine created falls short of its hardened profile."""
    problems: list[str] = []
    if effective.user != expected_user:
        problems.append(f"runs as {effective.user or 'the image default'}, not {expected_user}")
    if effective.privileged:
        problems.append("is privileged")
    if "ALL" not in effective.cap_drop:
        problems.append("does not drop every capability")
    if effective.cap_add:
        problems.append("adds capabilities")
    if "no-new-privileges" not in effective.security_options:
        problems.append("does not set no-new-privileges")
    if not effective.read_only_rootfs:
        problems.append("has a writable root filesystem")
    if effective.memory_bytes != limits.memory_bytes:
        problems.append("does not enforce its memory limit")
    if effective.memory_swap_bytes != limits.memory_bytes:
        problems.append("does not enforce its swap limit")
    if effective.nano_cpus != limits.nano_cpus:
        problems.append("does not enforce its cpu limit")
    if effective.pids_limit != limits.pids:
        problems.append("does not enforce its pids limit")
    if effective.devices:
        problems.append("maps host devices")
    for kind, destination, writable in effective.mounts:
        if kind == "tmpfs":
            continue
        if (kind, destination, writable) != ("bind", RELAY_TOKEN_MOUNT, False):
            problems.append(f"has an unexpected {kind} mount at {destination}")
    if not any(m[1] == RELAY_TOKEN_MOUNT and not m[2] for m in effective.mounts):
        problems.append("does not mount the gateway token read-only")
    return tuple(problems)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class RelayManager:
    """Create, inspect and remove the relay container on one engine."""

    engine: OciEngine
    profile: RunnerProfile = field(default_factory=RunnerProfile)
    limits: ResourceLimits = RELAY_LIMITS
    egress_network: str = EGRESS_NETWORK
    container_name: str = RELAY_CONTAINER_NAME
    """Overridable so a test can run a relay beside an operator's without touching it."""
    sleep: Callable[[float], None] = _sleep

    # --- queries ---------------------------------------------------------------------------

    def status(self) -> RelayStatus:
        """Report the relay's state; a missing relay is a status, not an error."""
        try:
            container = self.engine.container_inspect(self.container_name)
        except ObjectNotFoundError:
            return RelayStatus(
                present=False,
                running=False,
                healthy=False,
                image_id=None,
                networks=(),
                upstream=None,
                detail="the model access relay container does not exist",
            )
        state = _mapping(container.get("State"))
        config = _mapping(container.get("Config"))
        labels = _mapping(config.get("Labels"))
        settings = _mapping(container.get("NetworkSettings"))
        networks = tuple(sorted(_mapping(settings.get("Networks"))))
        running = state.get("Running") is True
        image_id = _text(container.get("Image")) or None
        upstream = self._upstream_of(config)
        if labels.get(RELAY_LABEL) != "1":
            return RelayStatus(
                present=True,
                running=running,
                healthy=False,
                image_id=image_id,
                networks=networks,
                upstream=upstream,
                detail=f"a container named {self.container_name} exists but is not the relay",
            )
        problems = list(
            relay_violations(
                effective_from_inspect(container),
                expected_user=self._expected_user(),
                limits=self.limits,
            )
        )
        if self.profile.network not in networks:
            problems.append(f"is not attached to the internal network {self.profile.network}")
        if self.egress_network not in networks:
            problems.append(f"is not attached to the egress network {self.egress_network}")
        if problems:
            detail = "the relay container " + "; ".join(problems)
            return RelayStatus(True, running, False, image_id, networks, upstream, detail)
        if not running:
            return RelayStatus(
                True,
                False,
                False,
                image_id,
                networks,
                upstream,
                "the relay container is not running",
            )
        healthy, detail = self._probe_health()
        return RelayStatus(True, True, healthy, image_id, networks, upstream, detail)

    @staticmethod
    def _upstream_of(config: Mapping[str, object]) -> str | None:
        env = config.get("Env")
        if not isinstance(env, list):
            return None
        for entry in env:
            if isinstance(entry, str) and entry.startswith("RELAY_UPSTREAM="):
                return entry.partition("=")[2]
        return None

    def _probe_health(self) -> tuple[bool, str]:
        """Ask the relay for its own ``/healthz`` from inside its container."""
        completed = self.engine.run_cli(
            ("exec", self.container_name, "python3", "-c", _HEALTH_PROGRAM),
            timeout=_HEALTH_TIMEOUT_SECONDS,
        )
        if completed.returncode == 0:
            return True, "the relay answers its health check"
        reason = completed.stderr.strip().splitlines()[-1:] or ["no answer"]
        return False, f"the relay does not answer its health check: {reason[0][:300]}"

    # --- lifecycle -------------------------------------------------------------------------

    def up(
        self,
        *,
        relay_image: str,
        upstream: str,
        token_file: Path,
        recreate: bool = False,
        wait_seconds: float = _UP_WAIT_SECONDS,
    ) -> RelayStatus:
        """Start the relay, or confirm the running one is exactly what was asked for.

        An existing relay created from the same image, upstream and token file is left alone
        unless ``recreate`` is set (recreate after the gateway token was regenerated, because a
        single-file bind mount follows the file's inode). Anything else is replaced.
        """
        _, host, _ = validate_upstream(upstream)
        token = check_token_file(token_file)
        identity = self.engine.qualify()
        verify_relay_image(self.engine, relay_image)
        self.engine.ensure_internal_network(self.profile.network)
        self._ensure_egress_network()
        spec = self._spec_digest(relay_image, upstream, token)
        existing = self.status()
        if existing.present and not recreate and self._matches(spec):
            current = self._poll_healthy(existing, wait_seconds) if existing.running else existing
            if current.healthy:
                return current
        if existing.present:
            self._remove_relay()
        # A rootless engine resolves host.docker.internal itself (Lima maps it to the host); its
        # "host-gateway" is the bridge inside the engine's own network namespace, which is not
        # the host and would break the upstream.
        self._create(
            relay_image,
            upstream,
            token,
            spec,
            host_gateway=host == HOST_GATEWAY_NAME and identity.kind != KIND_ROOTLESS_DOCKER,
        )
        return self._await_healthy(wait_seconds)

    def down(self) -> RelayStatus:
        """Remove the relay container (the networks stay); idempotent."""
        self._remove_relay()
        return self.status()

    # --- internals -------------------------------------------------------------------------

    def _spec_digest(self, relay_image: str, upstream: str, token: Path) -> str:
        document = {
            "user": self._expected_user(),
            "image": relay_image,
            "upstream": upstream,
            "token_file": str(token),
            "internal_network": self.profile.network,
            "egress_network": self.egress_network,
            "limits": self.limits.to_state(),
        }
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _matches(self, spec: str) -> bool:
        try:
            container = self.engine.container_inspect(self.container_name)
        except ObjectNotFoundError:
            return False
        labels = _mapping(_mapping(container.get("Config")).get("Labels"))
        return labels.get(RELAY_LABEL) == "1" and labels.get(RELAY_SPEC_LABEL) == spec

    def _remove_relay(self) -> None:
        try:
            container = self.engine.container_inspect(self.container_name)
        except ObjectNotFoundError:
            return
        labels = _mapping(_mapping(container.get("Config")).get("Labels"))
        if labels.get(RELAY_LABEL) != "1":
            raise MissingPrerequisiteError(
                f"a container named {self.container_name} exists but is not the relay; "
                "refusing to replace it"
            )
        self.engine.remove(self.container_name)

    def _ensure_egress_network(self) -> None:
        """Create the egress bridge network, or verify it is a non-internal one."""
        name = self.egress_network
        for attempt in range(2):
            completed = self.engine.run_cli(
                ("network", "inspect", name), timeout=_COMMAND_TIMEOUT_SECONDS
            )
            if completed.returncode == 0:
                try:
                    networks = json.loads(completed.stdout)
                except ValueError as error:
                    raise EngineCommandError(
                        "container engine reported unreadable network data"
                    ) from error
                for network in networks if isinstance(networks, list) else ():
                    if _mapping(network).get("Name") == name:
                        if _mapping(network).get("Internal") is True:
                            raise MissingPrerequisiteError(
                                f"egress network {name!r} is internal; the relay would have no "
                                "route to the gateway",
                                details={"network": name},
                            )
                        return
            if attempt == 1:
                break
            self.engine.run_cli(
                ("network", "create", "--label", f"{RUNNER_LABEL}=1", name),
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        raise EngineCommandError(f"egress network {name!r} is absent right after creation")

    def _expected_user(self) -> str:
        """The user the relay runs as: the worker uid, or mapped root on a rootless engine.

        A rootless engine maps container root onto the invoking user and every other uid onto
        an unprivileged sub-uid that cannot read the operator's private (0600) token file.
        Container root there holds no host privilege, and the relay still drops every
        capability and runs with no-new-privileges on a read-only filesystem.
        """
        identity = self.engine.identity()
        if identity.rootless:
            return ROOTLESS_RELAY_USER
        if identity.kind == KIND_DOCKER:
            # Plain Docker on Linux keeps real host uids: the private (0600) token file is
            # readable only by its owner, who is checked to be the invoking user. Running as
            # that unprivileged user (never root) lets the relay read it and nothing else.
            if os.geteuid() == 0:
                raise MissingPrerequisiteError(
                    "the relay would have to run as root to read a root-owned token file; "
                    "run the operator command as an unprivileged user"
                )
            return f"{os.geteuid()}:{os.getegid()}"
        return f"{self.profile.uid}:{self.profile.gid}"

    def _create(
        self, relay_image: str, upstream: str, token: Path, spec: str, *, host_gateway: bool
    ) -> None:
        profile = RunnerProfile(resources=self.limits, network=self.profile.network)
        user = self._expected_user()
        hardening = list(hardening_arguments(profile, network=self.profile.network))
        hardening[hardening.index("--user") + 1] = user
        arguments = [
            "create",
            *hardening,
            "--network-alias",
            RELAY_HOSTNAME,
            "--restart",
            "unless-stopped",
            "-v",
            f"{token}:{RELAY_TOKEN_MOUNT}:ro",
            "--label",
            f"{RELAY_LABEL}=1",
            "--label",
            f"{RELAY_SPEC_LABEL}={spec}",
            "--name",
            self.container_name,
            "-e",
            f"RELAY_UPSTREAM={upstream}",
            "-e",
            f"RELAY_TOKEN_FILE={RELAY_TOKEN_MOUNT}",
            "-e",
            "RELAY_LISTEN_HOST=0.0.0.0",
            "-e",
            f"RELAY_LISTEN_PORT={RELAY_PORT}",
        ]
        if host_gateway:
            arguments += ["--add-host", f"{HOST_GATEWAY_NAME}:host-gateway"]
        arguments.append(relay_image)
        self._checked(arguments)
        try:
            container = self.engine.container_inspect(self.container_name)
            problems = relay_violations(
                effective_from_inspect(container), expected_user=user, limits=self.limits
            )
            if problems:
                raise MissingPrerequisiteError(
                    "the engine created a relay container that is not hardened: "
                    + "; ".join(problems)
                )
            self._checked(("network", "connect", self.egress_network, self.container_name))
            self.engine.start(self.container_name)
        except Exception:
            self.engine.remove(self.container_name)
            raise

    def _checked(self, arguments: list[str] | tuple[str, ...]) -> None:
        completed = self.engine.run_cli(arguments, timeout=_COMMAND_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise EngineCommandError(
                f"container engine command failed: {' '.join(arguments[:2])}",
                details={
                    "returncode": str(completed.returncode),
                    "stderr": completed.stderr.strip()[-2000:],
                },
            )

    def _poll_healthy(self, current: RelayStatus, wait_seconds: float) -> RelayStatus:
        remaining = wait_seconds
        while not current.healthy and remaining > 0:
            self.sleep(_POLL_SECONDS)
            remaining -= _POLL_SECONDS
            current = self.status()
        return current

    def _await_healthy(self, wait_seconds: float) -> RelayStatus:
        current = self._poll_healthy(self.status(), wait_seconds)
        if not current.healthy:
            logs = ""
            with contextlib.suppress(MissingPrerequisiteError):
                logs = self.engine.logs_tail(self.container_name, lines=40, max_bytes=4096)
            self.engine.remove(self.container_name)
            raise MissingPrerequisiteError(
                f"the relay did not become healthy: {current.detail}",
                details={"logs": logs.strip()[-2000:]},
            )
        return current


__all__ = [
    "EGRESS_NETWORK",
    "RELAY_CONTAINER_NAME",
    "RELAY_LABEL",
    "RELAY_LIMITS",
    "RELAY_SPEC_LABEL",
    "RELAY_TOKEN_MOUNT",
    "RelayManager",
    "RelayStatus",
    "check_token_file",
    "relay_violations",
    "validate_upstream",
]
