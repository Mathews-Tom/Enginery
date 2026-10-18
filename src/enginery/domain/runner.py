"""Trust modes, runner profiles, and the receipts that record what actually ran.

A run executes in exactly one declared trust mode. ``trusted-local`` is the weaker mode that
has always existed: the worker runs as the operator's own user with the operator's
filesystem, and it is never containment. ``isolated-oci`` runs the worker in a hardened,
task-local OCI container that holds none of the operator's authority. A run that requires
isolation and cannot get a qualified runner is held; it is never run as ``trusted-local``.

Everything here is pure data and pure checks. Talking to a container engine, building
images, and launching containers belong to the OCI adapter.
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

RUNNER_RECEIPT_SCHEMA_VERSION = 1
RUNNER_SELECTION_SCHEMA_VERSION = 1

WORKSPACE_MOUNT = "/workspace"
"""Where the admitted task-local workspace appears inside the container (read-write)."""

OUT_MOUNT = "/out"
"""Where the per-attempt result/output directory appears inside the container (read-write)."""

WORKER_HOME = "/home/worker"
"""The worker's home: tmpfs, empty at every start."""

WORKER_TMP = "/tmp"
"""The worker's scratch space: tmpfs, empty at every start."""

ISOLATED_PROFILE_NAME = "enginery-isolated-oci-v1"
TRUSTED_LOCAL_PROFILE_NAME = "enginery-trusted-local-v1"

MODEL_ACCESS_NONE = "none"
MODEL_ACCESS_RELAY = "relay"
MODEL_ACCESS_MODES: tuple[str, ...] = (MODEL_ACCESS_NONE, MODEL_ACCESS_RELAY)
"""How an isolated worker reaches a model: not at all, or only through the operator's relay."""

RELAY_HOSTNAME = "enginery-relay"
"""The relay's network alias on each attempt's own network (also its container name)."""

RELAY_PORT = 8080
RELAY_DUMMY_CREDENTIAL = "enginery-relay"
"""The only credential a worker holds: a fixed, non-secret placeholder the relay never honors."""

NO_NETWORK = "none"
"""The engine network mode of a container that has a loopback interface and nothing else."""

_ATTEMPT_NETWORK_DIGEST_CHARS = 24

_RELAY_URL_PATTERN = re.compile(r"^http://[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]{1,5}$")

_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class TrustMode(enum.Enum):
    """How much a worker is separated from the operator's authority."""

    TRUSTED_LOCAL = "trusted-local"
    ISOLATED_OCI = "isolated-oci"


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise InvalidInputError(f"{name} must be non-blank")


def _mapping(state: object, name: str) -> Mapping[str, object]:
    if not isinstance(state, Mapping):
        raise InvalidInputError(f"{name} must be a mapping")
    return state


def _text(state: Mapping[str, object], key: str, name: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{name} {key} must be non-blank text")
    return value


def _optional_text(state: Mapping[str, object], key: str, name: str) -> str | None:
    value = state.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{name} {key} must be null or non-blank text")
    return value


def _integer(state: Mapping[str, object], key: str, name: str) -> int:
    value = state.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidInputError(f"{name} {key} must be an integer")
    return value


def _texts(state: Mapping[str, object], key: str, name: str) -> tuple[str, ...]:
    value = state.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidInputError(f"{name} {key} must be a list of text")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class PermissionMapping:
    """The exact permission flags one harness is launched with under one trust mode.

    The mapping is recorded in every receipt. ``unconditional_bypass`` is True only for a
    mapping that turns the harness's permission checks off entirely; the isolated profile
    never carries one.
    """

    harness: str
    name: str
    arguments: tuple[str, ...]
    unconditional_bypass: bool
    rationale: str

    def __post_init__(self) -> None:
        _require_text(self.harness, "permission mapping harness")
        _require_text(self.name, "permission mapping name")
        _require_text(self.rationale, "permission mapping rationale")
        if any(not argument.strip() for argument in self.arguments):
            raise InvalidInputError("permission mapping arguments must be non-blank")

    def to_state(self) -> dict[str, object]:
        return {
            "harness": self.harness,
            "name": self.name,
            "arguments": list(self.arguments),
            "unconditional_bypass": self.unconditional_bypass,
            "rationale": self.rationale,
        }

    @classmethod
    def from_state(cls, state: object) -> PermissionMapping:
        data = _mapping(state, "permission mapping")
        bypass = data.get("unconditional_bypass")
        if not isinstance(bypass, bool):
            raise InvalidInputError("permission mapping unconditional_bypass must be a boolean")
        return cls(
            harness=_text(data, "harness", "permission mapping"),
            name=_text(data, "name", "permission mapping"),
            arguments=_texts(data, "arguments", "permission mapping"),
            unconditional_bypass=bypass,
            rationale=_text(data, "rationale", "permission mapping"),
        )


_DISK_BOUND_DEFAULTS: dict[str, int] = {
    "workspace_mib": 4096,
    "out_mib": 1024,
    "workspace_inodes": 500_000,
    "out_inodes": 100_000,
}


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Hard container limits; every one is enforced by the engine and observed back."""

    memory_mib: int = 4096
    cpus: float = 2.0
    pids: int = 512
    tmp_mib: int = 512
    home_mib: int = 512
    stop_grace_seconds: int = 10
    workspace_mib: int = 4096
    out_mib: int = 1024
    workspace_inodes: int = 500_000
    out_inodes: int = 100_000

    def __post_init__(self) -> None:
        if self.memory_mib < 64 or self.pids < 8 or self.cpus <= 0:
            raise InvalidInputError("runner resource limits must be positive and usable")
        if self.tmp_mib < 1 or self.home_mib < 1 or self.stop_grace_seconds < 1:
            raise InvalidInputError("runner scratch sizes and stop grace must be positive")
        if min(self.workspace_mib, self.out_mib, self.workspace_inodes, self.out_inodes) < 1:
            raise InvalidInputError("runner workspace and result bounds must be positive")

    @property
    def memory_bytes(self) -> int:
        return self.memory_mib * 1024 * 1024

    @property
    def nano_cpus(self) -> int:
        return round(self.cpus * 1_000_000_000)

    def to_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "memory_mib": self.memory_mib,
            "cpus": self.cpus,
            "pids": self.pids,
            "tmp_mib": self.tmp_mib,
            "home_mib": self.home_mib,
            "stop_grace_seconds": self.stop_grace_seconds,
        }
        # The disk bounds are omitted at their defaults so a profile persisted before they
        # existed keeps its digest; a non-default bound is always part of the state.
        for key, default in _DISK_BOUND_DEFAULTS.items():
            value = getattr(self, key)
            if value != default:
                state[key] = value
        return state

    @classmethod
    def from_state(cls, state: object) -> ResourceLimits:
        data = _mapping(state, "resource limits")
        cpus = data.get("cpus")
        if not isinstance(cpus, int | float) or isinstance(cpus, bool):
            raise InvalidInputError("resource limits cpus must be a number")
        bounds = {
            key: _integer(data, key, "resource limits") if key in data else default
            for key, default in _DISK_BOUND_DEFAULTS.items()
        }
        return cls(
            memory_mib=_integer(data, "memory_mib", "resource limits"),
            cpus=float(cpus),
            pids=_integer(data, "pids", "resource limits"),
            tmp_mib=_integer(data, "tmp_mib", "resource limits"),
            home_mib=_integer(data, "home_mib", "resource limits"),
            stop_grace_seconds=_integer(data, "stop_grace_seconds", "resource limits"),
            **bounds,
        )


# Environment keys a container may receive. Anything else is refused before launch, so no
# ambient host variable (SCM, factory, broker, cloud, SSH) can reach a worker by omission.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "HOME",
    "PATH",
    "LANG",
    "TMPDIR",
    "TERM",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "DISABLE_TELEMETRY",
    "DISABLE_AUTOUPDATER",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "PI_CODING_AGENT_DIR",
    "PI_CONFIG_DIR",
)


@dataclass(frozen=True, slots=True)
class RunnerProfile:
    """The fixed hardening an isolated worker container runs under."""

    name: str = ISOLATED_PROFILE_NAME
    uid: int = 65532
    gid: int = 65532
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    cap_drop: tuple[str, ...] = ("ALL",)
    security_options: tuple[str, ...] = ("no-new-privileges",)
    read_only_rootfs: bool = True
    network: str = "enginery-runner-internal"
    """The name prefix of the per-attempt internal networks (see ``attempt_network_name``).

    No container is ever attached to a network of exactly this name: a model-access worker joins
    ``<network>-<24 hex of its operation id>`` (internal, created for that one attempt, shared
    only with the relay) and every other container runs with no network at all.
    """
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST

    def __post_init__(self) -> None:
        # The allowlist is a set: its order must not change the profile digest or a round trip.
        object.__setattr__(self, "env_allowlist", tuple(sorted(set(self.env_allowlist))))
        self._validate()

    def _validate(self) -> None:
        _require_text(self.name, "runner profile name")
        _require_text(self.network, "runner profile network")
        if self.uid < 1000 or self.gid < 1000:
            raise InvalidInputError("an isolated worker must run as a non-root uid and gid")
        if self.cap_drop != ("ALL",):
            raise InvalidInputError("an isolated worker must drop every capability")
        if "no-new-privileges" not in self.security_options:
            raise InvalidInputError("an isolated worker must set no-new-privileges")
        if not self.read_only_rootfs:
            raise InvalidInputError("an isolated worker must have a read-only root filesystem")

    def to_state(self) -> dict[str, object]:
        return {
            "name": self.name,
            "uid": self.uid,
            "gid": self.gid,
            "resources": self.resources.to_state(),
            "cap_drop": list(self.cap_drop),
            "security_options": list(self.security_options),
            "read_only_rootfs": self.read_only_rootfs,
            "network": self.network,
            "env_allowlist": sorted(self.env_allowlist),
            "mounts": {"workspace": WORKSPACE_MOUNT, "out": OUT_MOUNT},
        }

    @classmethod
    def from_state(cls, state: object) -> RunnerProfile:
        data = _mapping(state, "runner profile")
        read_only = data.get("read_only_rootfs")
        if not isinstance(read_only, bool):
            raise InvalidInputError("runner profile read_only_rootfs must be a boolean")
        return cls(
            name=_text(data, "name", "runner profile"),
            uid=_integer(data, "uid", "runner profile"),
            gid=_integer(data, "gid", "runner profile"),
            resources=ResourceLimits.from_state(data.get("resources")),
            cap_drop=_texts(data, "cap_drop", "runner profile"),
            security_options=_texts(data, "security_options", "runner profile"),
            read_only_rootfs=read_only,
            network=_text(data, "network", "runner profile"),
            env_allowlist=_texts(data, "env_allowlist", "runner profile"),
        )

    @property
    def digest(self) -> Digest:
        return Digest.of_json(self.to_state())


def attempt_network_name(profile: RunnerProfile, operation_id: str) -> str:
    """The deterministic name of the internal network created for one operation's attempt.

    Derived from the operation id, so a relaunch of the same operation finds (and replaces) the
    network a crashed attempt left behind, and two operations can never share one.
    """
    _require_text(operation_id, "operation id")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{profile.network}-{digest[:_ATTEMPT_NETWORK_DIGEST_CHARS]}"


def expected_network(profile: RunnerProfile, operation_id: str, *, relay: bool) -> str:
    """The one network an attempt's container may be attached to.

    A worker with model access joins its own per-attempt internal network (shared only with the
    relay for the attempt's lifetime); every other container, including every validation
    container and every worker without model access, has no network.
    """
    return attempt_network_name(profile, operation_id) if relay else NO_NETWORK


@dataclass(frozen=True, slots=True)
class RunnerSelection:
    """The trust mode a run is bound to, persisted in the run request.

    A request persisted before trust modes existed carries no selection: it decodes as
    ``legacy`` trusted-local and keeps its original request digest. Every new request binds
    its mode explicitly. An isolated selection pins the exact image id and the profile digest
    the run was admitted under; neither is ever re-resolved from a mutable name.
    """

    trust_mode: TrustMode
    profile_name: str
    profile_digest: Digest | None = None
    image: str | None = None
    docker_context: str | None = None
    legacy: bool = False
    model_access: str = MODEL_ACCESS_NONE

    def __post_init__(self) -> None:
        _require_text(self.profile_name, "runner selection profile")
        if self.trust_mode is TrustMode.ISOLATED_OCI:
            if self.legacy:
                raise InvalidInputError("an isolated selection cannot be a legacy selection")
            if self.profile_digest is None:
                raise InvalidInputError("an isolated selection must pin its profile digest")
            if self.image is None or not _IMAGE_ID_PATTERN.fullmatch(self.image):
                raise InvalidInputError(
                    "an isolated selection must pin an exact image id (sha256:<64 hex>)"
                )
        elif self.image is not None or self.profile_digest is not None or self.docker_context:
            raise InvalidInputError("a trusted-local selection carries no image, profile or engine")
        if self.docker_context is not None and not self.docker_context.strip():
            raise InvalidInputError("docker context must be null or non-blank")
        if self.model_access not in MODEL_ACCESS_MODES:
            raise InvalidInputError(
                "runner selection model access is unknown",
                details={"model_access": self.model_access},
            )
        if self.model_access != MODEL_ACCESS_NONE and self.trust_mode is not TrustMode.ISOLATED_OCI:
            raise InvalidInputError("model access through the relay applies only to isolated runs")

    @classmethod
    def trusted_local(cls, *, legacy: bool = False) -> RunnerSelection:
        return cls(
            trust_mode=TrustMode.TRUSTED_LOCAL,
            profile_name=TRUSTED_LOCAL_PROFILE_NAME,
            legacy=legacy,
        )

    @classmethod
    def isolated(
        cls,
        *,
        image: str,
        profile: RunnerProfile,
        docker_context: str | None = None,
        model_access: str = MODEL_ACCESS_NONE,
    ) -> RunnerSelection:
        return cls(
            trust_mode=TrustMode.ISOLATED_OCI,
            profile_name=profile.name,
            profile_digest=profile.digest,
            image=image,
            docker_context=docker_context,
            model_access=model_access,
        )

    @property
    def isolated_mode(self) -> bool:
        return self.trust_mode is TrustMode.ISOLATED_OCI

    def to_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "schema_version": RUNNER_SELECTION_SCHEMA_VERSION,
            "trust_mode": self.trust_mode.value,
            "profile": self.profile_name,
            "profile_digest": None if self.profile_digest is None else str(self.profile_digest),
            "image": self.image,
            "docker_context": self.docker_context,
        }
        if self.model_access != MODEL_ACCESS_NONE:
            # Absent means no model access, so a selection without it keeps its persisted shape.
            state["model_access"] = self.model_access
        return state

    @classmethod
    def from_state(cls, state: object) -> RunnerSelection:
        data = _mapping(state, "runner selection")
        if data.get("schema_version") != RUNNER_SELECTION_SCHEMA_VERSION:
            raise InvalidInputError("runner selection schema version is unsupported")
        raw_mode = _text(data, "trust_mode", "runner selection")
        try:
            mode = TrustMode(raw_mode)
        except ValueError as error:
            raise InvalidInputError("runner selection trust mode is unknown") from error
        raw_digest = _optional_text(data, "profile_digest", "runner selection")
        digest = None
        if raw_digest is not None:
            algorithm, _, hex_value = raw_digest.partition(":")
            digest = Digest(algorithm, hex_value)
        return cls(
            trust_mode=mode,
            profile_name=_text(data, "profile", "runner selection"),
            profile_digest=digest,
            image=_optional_text(data, "image", "runner selection"),
            docker_context=_optional_text(data, "docker_context", "runner selection"),
            model_access=_optional_text(data, "model_access", "runner selection")
            or MODEL_ACCESS_NONE,
        )


LEGACY_RUNNER_SELECTION = RunnerSelection.trusted_local(legacy=True)


@dataclass(frozen=True, slots=True)
class EngineIdentity:
    """The container engine a worker actually ran on, as the engine itself reported it."""

    kind: str
    server_version: str
    operating_system: str
    kernel_version: str
    cgroup_version: str
    security_options: tuple[str, ...]
    rootless: bool
    context: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.kind, "engine kind")
        _require_text(self.server_version, "engine server version")
        _require_text(self.operating_system, "engine operating system")
        _require_text(self.kernel_version, "engine kernel version")
        _require_text(self.cgroup_version, "engine cgroup version")

    def to_state(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "server_version": self.server_version,
            "operating_system": self.operating_system,
            "kernel_version": self.kernel_version,
            "cgroup_version": self.cgroup_version,
            "security_options": list(self.security_options),
            "rootless": self.rootless,
            "context": self.context,
        }

    @classmethod
    def from_state(cls, state: object) -> EngineIdentity:
        data = _mapping(state, "engine identity")
        rootless = data.get("rootless")
        if not isinstance(rootless, bool):
            raise InvalidInputError("engine identity rootless must be a boolean")
        return cls(
            kind=_text(data, "kind", "engine identity"),
            server_version=_text(data, "server_version", "engine identity"),
            operating_system=_text(data, "operating_system", "engine identity"),
            kernel_version=_text(data, "kernel_version", "engine identity"),
            cgroup_version=_text(data, "cgroup_version", "engine identity"),
            security_options=_texts(data, "security_options", "engine identity"),
            rootless=rootless,
            context=_optional_text(data, "context", "engine identity"),
        )


@dataclass(frozen=True, slots=True)
class ImageIdentity:
    """The pinned worker image: its exact id and the recipe it was built from."""

    image_id: str
    recipe_digest: str
    harness_versions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _IMAGE_ID_PATTERN.fullmatch(self.image_id):
            raise InvalidInputError("image id must be sha256:<64 hex>")
        _require_text(self.recipe_digest, "image recipe digest")

    def to_state(self) -> dict[str, object]:
        return {
            "image_id": self.image_id,
            "recipe_digest": self.recipe_digest,
            "harness_versions": dict(self.harness_versions),
        }

    @classmethod
    def from_state(cls, state: object) -> ImageIdentity:
        data = _mapping(state, "image identity")
        versions = data.get("harness_versions")
        if not isinstance(versions, Mapping):
            raise InvalidInputError("image identity harness_versions must be a mapping")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in versions.items()
        ):
            raise InvalidInputError("image identity harness_versions must map text to text")
        return cls(
            image_id=_text(data, "image_id", "image identity"),
            recipe_digest=_text(data, "recipe_digest", "image identity"),
            harness_versions=tuple(sorted((str(k), str(v)) for k, v in versions.items())),
        )


@dataclass(frozen=True, slots=True)
class EffectiveContainer:
    """What the engine reports the container was actually created with.

    Read back from the engine after creation, never copied from the requested flags: the
    receipt proves what is enforced, and a mismatch with the profile refuses the launch.
    """

    user: str
    cap_drop: tuple[str, ...]
    cap_add: tuple[str, ...]
    security_options: tuple[str, ...]
    privileged: bool
    read_only_rootfs: bool
    memory_bytes: int
    memory_swap_bytes: int
    nano_cpus: int
    pids_limit: int
    network_mode: str
    networks: tuple[str, ...]
    mounts: tuple[tuple[str, str, bool], ...]
    """``(kind, destination, writable)`` for every mount; the host source is never recorded."""
    devices: tuple[str, ...]
    env_keys: tuple[str, ...]
    """Names only; values are never recorded."""

    def to_state(self) -> dict[str, object]:
        return {
            "user": self.user,
            "cap_drop": list(self.cap_drop),
            "cap_add": list(self.cap_add),
            "security_options": list(self.security_options),
            "privileged": self.privileged,
            "read_only_rootfs": self.read_only_rootfs,
            "memory_bytes": self.memory_bytes,
            "memory_swap_bytes": self.memory_swap_bytes,
            "nano_cpus": self.nano_cpus,
            "pids_limit": self.pids_limit,
            "network_mode": self.network_mode,
            "networks": list(self.networks),
            "mounts": [
                {"kind": kind, "destination": destination, "writable": writable}
                for kind, destination, writable in self.mounts
            ],
            "devices": list(self.devices),
            "env_keys": sorted(self.env_keys),
        }

    @classmethod
    def from_state(cls, state: object) -> EffectiveContainer:
        data = _mapping(state, "effective container")
        mounts_raw = data.get("mounts")
        if not isinstance(mounts_raw, list):
            raise InvalidInputError("effective container mounts must be a list")
        mounts: list[tuple[str, str, bool]] = []
        for item in mounts_raw:
            entry = _mapping(item, "effective container mount")
            writable = entry.get("writable")
            if not isinstance(writable, bool):
                raise InvalidInputError("effective container mount writable must be a boolean")
            mounts.append(
                (
                    _text(entry, "kind", "effective container mount"),
                    _text(entry, "destination", "effective container mount"),
                    writable,
                )
            )
        flags = {key: data.get(key) for key in ("privileged", "read_only_rootfs")}
        if not all(isinstance(value, bool) for value in flags.values()):
            raise InvalidInputError("effective container flags must be booleans")
        return cls(
            user=_text(data, "user", "effective container"),
            cap_drop=_texts(data, "cap_drop", "effective container"),
            cap_add=_texts(data, "cap_add", "effective container"),
            security_options=_texts(data, "security_options", "effective container"),
            privileged=bool(flags["privileged"]),
            read_only_rootfs=bool(flags["read_only_rootfs"]),
            memory_bytes=_integer(data, "memory_bytes", "effective container"),
            memory_swap_bytes=_integer(data, "memory_swap_bytes", "effective container"),
            nano_cpus=_integer(data, "nano_cpus", "effective container"),
            pids_limit=_integer(data, "pids_limit", "effective container"),
            network_mode=_text(data, "network_mode", "effective container"),
            networks=_texts(data, "networks", "effective container"),
            mounts=tuple(mounts),
            devices=_texts(data, "devices", "effective container"),
            env_keys=_texts(data, "env_keys", "effective container"),
        )


def profile_violations(
    profile: RunnerProfile, effective: EffectiveContainer, *, network: str
) -> tuple[str, ...]:
    """Return every way the observed container is weaker than the profile; empty means exact.

    A launch with any violation is refused and the container removed. The checks are the
    acceptance criteria for an isolated worker that ``EffectiveContainer`` can express:
    non-root, no capabilities, no privilege escalation, no security option beyond the profile's
    (so no ``seccomp=unconfined``, ``apparmor=unconfined``, ``label=disable`` or
    ``systempaths=unconfined``), read-only rootfs, enforced memory/swap/cpu/pids limits, exactly
    the one ``network`` it was launched for (``expected_network``: the attempt's own internal
    network or ``none``; never the default bridge, another attempt's network or a shared one),
    no devices, no unadmitted mount, and no environment outside the allowlist.
    The facts the engine reports outside ``EffectiveContainer`` (namespaces, masked paths,
    sysctls, runtime, tmpfs sizes, log and ulimit bounds) are checked by the launcher's
    ``launch_violations``.
    """
    problems: list[str] = []
    expected_user = f"{profile.uid}:{profile.gid}"
    if effective.user != expected_user:
        problems.append(f"user is {effective.user!r}, expected {expected_user!r}")
    if effective.privileged:
        problems.append("container is privileged")
    if set(effective.cap_drop) != set(profile.cap_drop):
        problems.append("capabilities are not all dropped")
    if effective.cap_add:
        problems.append("container adds capabilities")
    for option in profile.security_options:
        if option not in effective.security_options:
            problems.append(f"security option {option!r} is not set")
    for option in effective.security_options:
        if option not in profile.security_options:
            problems.append(f"security option {option!r} is outside the profile")
    if not effective.read_only_rootfs:
        problems.append("root filesystem is writable")
    limits = profile.resources
    if effective.memory_bytes != limits.memory_bytes:
        problems.append("memory limit is not the profile limit")
    if effective.memory_swap_bytes != limits.memory_bytes:
        problems.append("swap is not disabled (memory-swap differs from memory)")
    if effective.nano_cpus != limits.nano_cpus:
        problems.append("cpu limit is not the profile limit")
    if effective.pids_limit != limits.pids:
        problems.append("pids limit is not the profile limit")
    if effective.network_mode != network or effective.networks != (network,):
        problems.append(f"container is not attached only to the network {network!r}")
    if effective.devices:
        problems.append("container has host devices")
    allowed_mounts = {
        ("volume", WORKSPACE_MOUNT, True),
        ("volume", OUT_MOUNT, True),
        ("tmpfs", WORKER_TMP, True),
        ("tmpfs", WORKER_HOME, True),
    }
    for mount in effective.mounts:
        if mount not in allowed_mounts:
            problems.append(f"unadmitted mount {mount[0]} at {mount[1]}")
    stored = {mount[1] for mount in effective.mounts if mount[0] == "volume"}
    if stored != {WORKSPACE_MOUNT, OUT_MOUNT}:
        problems.append("the workspace and result mounts are not exactly the admitted volumes")
    stray_env = sorted(set(effective.env_keys) - set(profile.env_allowlist) - _IMAGE_ENV_KEYS)
    if stray_env:
        problems.append(f"environment keys outside the allowlist: {stray_env}")
    return tuple(problems)


# Keys the engine and image add to every container's environment by themselves.
_IMAGE_ENV_KEYS = frozenset({"HOSTNAME", "NODE_VERSION", "YARN_VERSION", "BUN_INSTALL", "PWD"})


DISK_BOUND_KERNEL_VOLUME = "kernel-capped-volume"
"""Disk bound mechanism: the workspace and result trees are tmpfs volumes whose size and inode
count the kernel enforces, so a write beyond the cap fails with ``ENOSPC`` inside the container.
Pages a worker writes into them are charged to the container's memory cgroup, which is therefore
the ceiling on what the worker can add; the content the host seeded is held by the engine and
charged to no container."""


@dataclass(frozen=True, slots=True)
class ResourceBounds:
    """The disk and log bounds one attempt ran under, and the mechanism that enforces them."""

    disk_mechanism: str
    workspace_bytes: int
    out_bytes: int
    workspace_inodes: int
    out_inodes: int
    max_file_bytes: int
    log_driver: str
    log_max_size: str
    log_max_file: int

    def __post_init__(self) -> None:
        _require_text(self.disk_mechanism, "resource bounds disk mechanism")
        _require_text(self.log_driver, "resource bounds log driver")
        _require_text(self.log_max_size, "resource bounds log size")

    def to_state(self) -> dict[str, object]:
        return {
            "disk_mechanism": self.disk_mechanism,
            "workspace_bytes": self.workspace_bytes,
            "out_bytes": self.out_bytes,
            "workspace_inodes": self.workspace_inodes,
            "out_inodes": self.out_inodes,
            "max_file_bytes": self.max_file_bytes,
            "log_driver": self.log_driver,
            "log_max_size": self.log_max_size,
            "log_max_file": self.log_max_file,
        }

    @classmethod
    def from_state(cls, state: object) -> ResourceBounds:
        data = _mapping(state, "resource bounds")
        return cls(
            disk_mechanism=_text(data, "disk_mechanism", "resource bounds"),
            workspace_bytes=_integer(data, "workspace_bytes", "resource bounds"),
            out_bytes=_integer(data, "out_bytes", "resource bounds"),
            workspace_inodes=_integer(data, "workspace_inodes", "resource bounds"),
            out_inodes=_integer(data, "out_inodes", "resource bounds"),
            max_file_bytes=_integer(data, "max_file_bytes", "resource bounds"),
            log_driver=_text(data, "log_driver", "resource bounds"),
            log_max_size=_text(data, "log_max_size", "resource bounds"),
            log_max_file=_integer(data, "log_max_file", "resource bounds"),
        )


@dataclass(frozen=True, slots=True)
class RunnerReceipt:
    """Retained proof of what one isolated worker attempt actually ran under."""

    operation_id: str
    trust_mode: TrustMode
    profile_name: str
    profile_digest: Digest
    engine: EngineIdentity
    image: ImageIdentity
    container_name: str
    host: Mapping[str, str]
    effective: EffectiveContainer
    permissions: PermissionMapping
    harness_version: str | None
    harness_protocol: str | None
    started_at: str
    bounds: ResourceBounds | None = None

    def __post_init__(self) -> None:
        if self.trust_mode is TrustMode.ISOLATED_OCI and self.permissions.unconditional_bypass:
            raise InvalidInputError("an isolated runner receipt cannot record a permission bypass")

    def to_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "schema_version": RUNNER_RECEIPT_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "trust_mode": self.trust_mode.value,
            "profile": self.profile_name,
            "profile_digest": str(self.profile_digest),
            "engine": self.engine.to_state(),
            "image": self.image.to_state(),
            "container_name": self.container_name,
            "host": dict(self.host),
            "effective": self.effective.to_state(),
            "permissions": self.permissions.to_state(),
            "harness_version": self.harness_version,
            "harness_protocol": self.harness_protocol,
            "started_at": self.started_at,
        }
        if self.bounds is not None:  # omitted when absent so an earlier receipt keeps its digest
            state["bounds"] = self.bounds.to_state()
        return state

    @classmethod
    def from_state(cls, state: object) -> RunnerReceipt:
        data = _mapping(state, "runner receipt")
        if data.get("schema_version") != RUNNER_RECEIPT_SCHEMA_VERSION:
            raise InvalidInputError("runner receipt schema version is unsupported")
        host = _mapping(data.get("host"), "runner receipt host")
        raw_digest = _text(data, "profile_digest", "runner receipt")
        algorithm, _, hex_value = raw_digest.partition(":")
        try:
            mode = TrustMode(_text(data, "trust_mode", "runner receipt"))
        except ValueError as error:
            raise InvalidInputError("runner receipt trust mode is unknown") from error
        return cls(
            operation_id=_text(data, "operation_id", "runner receipt"),
            trust_mode=mode,
            profile_name=_text(data, "profile", "runner receipt"),
            profile_digest=Digest(algorithm, hex_value),
            engine=EngineIdentity.from_state(data.get("engine")),
            image=ImageIdentity.from_state(data.get("image")),
            container_name=_text(data, "container_name", "runner receipt"),
            host={str(key): str(value) for key, value in host.items()},
            effective=EffectiveContainer.from_state(data.get("effective")),
            permissions=PermissionMapping.from_state(data.get("permissions")),
            harness_version=_optional_text(data, "harness_version", "runner receipt"),
            harness_protocol=_optional_text(data, "harness_protocol", "runner receipt"),
            started_at=_text(data, "started_at", "runner receipt"),
            bounds=None
            if data.get("bounds") is None
            else ResourceBounds.from_state(data.get("bounds")),
        )

    @property
    def digest(self) -> Digest:
        return Digest.of_json(self.to_state())


OUTCOME_SCHEMA_VERSION = 1


def outcome_path_for(receipt_path: str) -> str:
    """Where the launcher records its own outcome, next to (and as private as) the receipt."""
    return f"{receipt_path}.outcome.json"


class OutcomeState(enum.Enum):
    """How one launcher run ended, as the launcher itself observed it."""

    LAUNCHING = "launching"
    RUNNING = "running"
    EXITED = "exited"
    REFUSED = "refused"
    DEADLINE = "deadline"
    INTERRUPTED = "interrupted"
    FAULTED = "faulted"


@dataclass(frozen=True, slots=True)
class LaunchOutcome:
    """What the host-side launcher itself concluded about one attempt.

    The launcher writes this to a host-only file outside every worker mount. Collection
    classifies an attempt from it, never from the launcher's process exit status (which a
    contained command can imitate: ``124`` and ``125`` are ordinary exit codes) and never from
    the worker-written result file. ``nonce`` is minted per launch and passed to the worker,
    which echoes it in its result so a result left by an earlier attempt cannot be replayed.
    """

    operation_id: str
    nonce: str
    state: OutcomeState
    exit_code: int | None = None
    detail: str = ""
    violations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.operation_id, "launch outcome operation id")
        _require_text(self.nonce, "launch outcome nonce")
        if (self.state is OutcomeState.EXITED) != (self.exit_code is not None):
            raise InvalidInputError("only an exited launch outcome carries an exit code")

    @property
    def final(self) -> bool:
        return self.state not in (OutcomeState.LAUNCHING, OutcomeState.RUNNING)

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "nonce": self.nonce,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "detail": self.detail,
            "violations": list(self.violations),
        }

    @classmethod
    def from_state(cls, state: object) -> LaunchOutcome:
        data = _mapping(state, "launch outcome")
        if data.get("schema_version") != OUTCOME_SCHEMA_VERSION:
            raise InvalidInputError("launch outcome schema version is unsupported")
        try:
            outcome_state = OutcomeState(_text(data, "state", "launch outcome"))
        except ValueError as error:
            raise InvalidInputError("launch outcome state is unknown") from error
        exit_code = data.get("exit_code")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise InvalidInputError("launch outcome exit code must be an integer or null")
        detail = data.get("detail", "")
        if not isinstance(detail, str):
            raise InvalidInputError("launch outcome detail must be text")
        return cls(
            operation_id=_text(data, "operation_id", "launch outcome"),
            nonce=_text(data, "nonce", "launch outcome"),
            state=outcome_state,
            exit_code=exit_code,
            detail=detail,
            violations=_texts(data, "violations", "launch outcome"),
        )


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything the host-side launcher needs to start one isolated container attempt.

    The spec is recorded in the supervised command line, so it carries no secret: ``env``
    values are non-secret configuration, never credentials.
    """

    operation_id: str
    container_name: str
    image: str
    profile: RunnerProfile
    harness: str
    workspace_path: str
    out_path: str
    receipt_path: str
    result_name: str
    time_budget_seconds: int
    permissions: PermissionMapping
    env: tuple[tuple[str, str], ...] = ()
    seed_files: tuple[tuple[str, str], ...] = ()
    bundle_branch: str | None = None
    bundle_base: str | None = None
    docker_context: str | None = None
    harness_version: str | None = None
    """The in-image harness version the adapter probed; the launcher records it."""
    harness_protocol: str | None = None
    """The in-image harness protocol fingerprint the adapter probed; the launcher records it."""
    relay_container: str | None = None
    """The relay container to attach to this attempt's own network, for a worker with model access.

    ``None`` (every validation container and every worker without model access) means the
    container runs with no network at all.
    """

    def __post_init__(self) -> None:
        for name, value in (
            ("operation id", self.operation_id),
            ("container name", self.container_name),
            ("harness", self.harness),
            ("result name", self.result_name),
        ):
            _require_text(value, f"launch spec {name}")
        if not _IMAGE_ID_PATTERN.fullmatch(self.image):
            raise InvalidInputError("launch spec image must be an exact image id")
        for path_name, path in (
            ("workspace", self.workspace_path),
            ("out", self.out_path),
            ("receipt", self.receipt_path),
        ):
            if not path.startswith("/"):
                raise InvalidInputError(f"launch spec {path_name} path must be absolute")
        if self.time_budget_seconds < 1:
            raise InvalidInputError("launch spec time budget must be positive")
        if "/" in self.result_name or self.result_name.startswith("."):
            raise InvalidInputError("launch spec result name must be a plain file name")
        if (self.bundle_branch is None) != (self.bundle_base is None):
            raise InvalidInputError("a bundle needs both its branch and its base revision")
        stray = sorted({key for key, _ in self.env} - set(self.profile.env_allowlist))
        if stray:
            raise InvalidInputError(
                "launch spec environment keys are outside the profile allowlist",
                details={"keys": ",".join(stray)},
            )
        if self.relay_container is not None and not _CONTAINER_NAME_PATTERN.fullmatch(
            self.relay_container
        ):
            raise InvalidInputError("launch spec relay container must be a plain container name")

    @property
    def attempt_network(self) -> str:
        """The one network this attempt's container joins: its own internal one, or ``none``."""
        return expected_network(
            self.profile, self.operation_id, relay=self.relay_container is not None
        )

    def to_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "operation_id": self.operation_id,
            "container_name": self.container_name,
            "image": self.image,
            "profile": self.profile.to_state(),
            "harness": self.harness,
            "workspace_path": self.workspace_path,
            "out_path": self.out_path,
            "receipt_path": self.receipt_path,
            "result_name": self.result_name,
            "time_budget_seconds": self.time_budget_seconds,
            "permissions": self.permissions.to_state(),
            "env": [[key, value] for key, value in self.env],
            "seed_files": [[path, content] for path, content in self.seed_files],
            "bundle_branch": self.bundle_branch,
            "bundle_base": self.bundle_base,
            "docker_context": self.docker_context,
        }
        # The probed in-image harness identity is recorded only when the adapter verified it.
        if self.harness_version is not None:
            state["harness_version"] = self.harness_version
        if self.harness_protocol is not None:
            state["harness_protocol"] = self.harness_protocol
        if self.relay_container is not None:
            state["relay_container"] = self.relay_container
        return state

    @classmethod
    def from_state(cls, state: object) -> LaunchSpec:
        data = _mapping(state, "launch spec")

        def pairs(key: str) -> tuple[tuple[str, str], ...]:
            raw = data.get(key)
            if not isinstance(raw, list):
                raise InvalidInputError(f"launch spec {key} must be a list")
            entries: list[tuple[str, str]] = []
            for item in raw:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not all(isinstance(part, str) for part in item)
                ):
                    raise InvalidInputError(f"launch spec {key} entries must be text pairs")
                entries.append((item[0], item[1]))
            return tuple(entries)

        return cls(
            operation_id=_text(data, "operation_id", "launch spec"),
            container_name=_text(data, "container_name", "launch spec"),
            image=_text(data, "image", "launch spec"),
            profile=RunnerProfile.from_state(data.get("profile")),
            harness=_text(data, "harness", "launch spec"),
            workspace_path=_text(data, "workspace_path", "launch spec"),
            out_path=_text(data, "out_path", "launch spec"),
            receipt_path=_text(data, "receipt_path", "launch spec"),
            result_name=_text(data, "result_name", "launch spec"),
            time_budget_seconds=_integer(data, "time_budget_seconds", "launch spec"),
            permissions=PermissionMapping.from_state(data.get("permissions")),
            env=pairs("env"),
            seed_files=pairs("seed_files"),
            bundle_branch=_optional_text(data, "bundle_branch", "launch spec"),
            bundle_base=_optional_text(data, "bundle_base", "launch spec"),
            docker_context=_optional_text(data, "docker_context", "launch spec"),
            harness_version=_optional_text(data, "harness_version", "launch spec"),
            harness_protocol=_optional_text(data, "harness_protocol", "launch spec"),
            relay_container=_optional_text(data, "relay_container", "launch spec"),
        )


@dataclass(frozen=True, slots=True)
class RelayEndpoint:
    """Where an isolated worker reaches the operator's model-access relay, and what it holds.

    The credential is a fixed placeholder that identifies nothing: the relay discards every
    inbound credential and injects the gateway bearer itself, so a worker never holds a secret.
    """

    url: str = f"http://{RELAY_HOSTNAME}:{RELAY_PORT}"
    dummy_credential: str = RELAY_DUMMY_CREDENTIAL

    def __post_init__(self) -> None:
        if not _RELAY_URL_PATTERN.fullmatch(self.url):
            raise InvalidInputError(
                "relay url must be http://host:port without path or credentials"
            )
        if not self.dummy_credential or not self.dummy_credential.isascii():
            raise InvalidInputError("relay dummy credential must be non-empty ASCII")
        if self.dummy_credential != RELAY_DUMMY_CREDENTIAL:
            raise InvalidInputError("a worker is only ever given the fixed relay placeholder")


@dataclass(frozen=True, slots=True)
class RunnerContext:
    """What a harness adapter needs to launch its worker in the isolated profile."""

    selection: RunnerSelection
    profile: RunnerProfile
    receipt_root: str
    """Absolute host directory the launcher writes receipts into (outside every worker mount)."""
    relay: RelayEndpoint | None = None
    """Set exactly when the selection grants model access through the relay."""

    def __post_init__(self) -> None:
        if not self.selection.isolated_mode:
            raise InvalidInputError("a runner context requires an isolated selection")
        if self.selection.profile_digest != self.profile.digest:
            raise InvalidInputError("runner context profile does not match its selection")
        if not self.receipt_root.startswith("/"):
            raise InvalidInputError("runner context receipt root must be absolute")
        if (self.relay is not None) != (self.selection.model_access == MODEL_ACCESS_RELAY):
            raise InvalidInputError(
                "a runner context carries a relay endpoint exactly when its selection grants "
                "model access through the relay"
            )


def isolated_permission_mapping(harness: str) -> PermissionMapping:
    """The explicit permission mapping the isolated profile launches ``harness`` with."""
    try:
        return _ISOLATED_PERMISSIONS[harness]
    except KeyError as error:
        raise InvalidInputError(
            "no isolated permission mapping exists for this harness", details={"harness": harness}
        ) from error


def trusted_local_permission_mapping(harness: str) -> PermissionMapping:
    """The explicit, declared-weaker mapping the trusted-local profile launches ``harness`` with.

    Both harnesses run with their permission checks off. That is the behaviour trusted-local
    has always had; it is now named here and recorded, not inherited implicitly.
    """
    try:
        return _TRUSTED_LOCAL_PERMISSIONS[harness]
    except KeyError as error:
        raise InvalidInputError(
            "no trusted-local permission mapping exists for this harness",
            details={"harness": harness},
        ) from error


_TRUSTED_LOCAL_PERMISSIONS: dict[str, PermissionMapping] = {
    "claude-code": PermissionMapping(
        harness="claude-code",
        name="claude-trusted-local-bypass",
        arguments=("--permission-mode", "bypassPermissions"),
        unconditional_bypass=True,
        rationale=(
            "trusted-local is explicitly weaker: the worker runs as the operator and is not "
            "contained, so Claude Code runs with permission checks off"
        ),
    ),
    "omp": PermissionMapping(
        harness="omp",
        name="omp-trusted-local-yolo",
        arguments=(),
        unconditional_bypass=True,
        rationale=(
            "trusted-local is explicitly weaker: omp's headless default approval mode is yolo "
            "and no approval flag is passed"
        ),
    ),
}

# The isolated mappings are explicit and never an unconditional bypass. Each was observed against
# the real harness binary refusing a disallowed tool call and running an allowed one.
#
# Claude Code runs with `--restricted` (file tools confined to the workspace; user, project and
# local settings files ignored, so a repository cannot widen the rules) and `dontAsk` with
# `--permission-prompts none` (anything not allowed is denied, nothing ever waits for an answer).
# Bash is allowed only for local version control: pushing, remotes, config, fetching and network
# tools are denied outright and the web tools are not offered. No entry runs a test runner or a
# linter: those execute code from the workspace (conftest, plugins, config), which a deny entry
# for publishing would not stop, and the worker image does not carry them. Git hooks are disabled
# for the worker through command-scoped configuration in its environment.
#
# These lists narrow the tool surface; they are not the security boundary. Allowed git commands
# still honour repository-local configuration (diff drivers, fsmonitor) that the worker can write.
# What keeps a worker from publishing or reaching anything is the container.
#
# omp has no path-scoped or flag-level allow-list: its fine-grained surface is settings. It runs
# with `--approval-mode write` (read and write tools are auto-approved, every other tool call is
# refused because a headless run has nobody to ask), a closed tool set, and a read-only settings
# overlay shipped in the worker image that allows only the same shell commands and wins over any
# repository setting. Its file tools are not path-confined; the container's mounts are what keep
# them inside the workspace.
ISOLATED_SHELL_COMMANDS = (
    "git status",
    "git add",
    "git commit",
    "git diff",
    "git log",
    "git show",
    "git switch",
    "git checkout -b",
    "git branch",
    "git rev-parse",
    "git ls-files",
)


_ISOLATED_PERMISSIONS: dict[str, PermissionMapping] = {
    "claude-code": PermissionMapping(
        harness="claude-code",
        name="claude-isolated-restricted-allowlist",
        arguments=(
            "--restricted",
            "--tools",
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--allowedTools",
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            *(f"Bash({command}:*)" for command in ISOLATED_SHELL_COMMANDS),
            "--disallowedTools",
            "Bash(git push:*)",
            "Bash(git remote:*)",
            "Bash(git config:*)",
            "Bash(git fetch:*)",
            "Bash(git pull:*)",
            "Bash(git clone:*)",
            "Bash(git bundle:*)",
            "Bash(curl:*)",
            "Bash(wget:*)",
            "Bash(ssh:*)",
            "WebFetch",
            "WebSearch",
        ),
        unconditional_bypass=False,
        rationale=(
            "restricted mode confines file tools to the workspace and ignores repository "
            "settings; dontAsk with no prompts denies everything not in the allow list; shell "
            "access is limited to local git commands and publishing and network tools are "
            "denied. This narrows the worker's tool surface (defence in depth); it is not the "
            "boundary: an allowed git command can still run code that the repository's own "
            "configuration names, and the file tools can write it. Containment is the "
            "container, which holds no credentials, no remote and no route out"
        ),
    ),
    "omp": PermissionMapping(
        harness="omp",
        name="omp-isolated-write-allowlist",
        arguments=(
            "--approval-mode",
            "write",
            "--config",
            "/opt/enginery/omp-isolated-policy.yml",
            "--tools",
            "read,edit,write,bash,glob,grep",
        ),
        unconditional_bypass=False,
        rationale=(
            "write approval mode refuses every tool call above workspace writes because a "
            "headless run cannot ask, and the read-only image overlay allows only local git "
            "commands and denies publishing and network commands. This narrows the worker's "
            "tool surface (defence in depth); it is not the boundary: an allowed git command "
            "can still run code that the repository's own configuration names, and the file "
            "tools can write it. Containment is the container, which holds no credentials, no "
            "remote and no route out"
        ),
    ),
}
