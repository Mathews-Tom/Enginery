"""Build the hardened container command lines and read back what the engine created.

``create_arguments`` is the only place the isolated worker's ``docker create`` command line
is assembled, and ``probe_arguments`` shares its hardening. ``effective_from_inspect`` turns
the engine's own report of the created container into the record the profile is checked
against, so a launch is judged by what the engine holds, not by what was requested.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence

from enginery.adapters.oci.engine import OPERATION_LABEL, PROBE_LABEL, RUNNER_LABEL, OciEngine
from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    OUT_MOUNT,
    WORKER_HOME,
    WORKER_TMP,
    WORKSPACE_MOUNT,
    EffectiveContainer,
    LaunchSpec,
    RunnerProfile,
)

WORKER_ENTRY_PATH = "/opt/enginery/worker_entry.py"
"""The in-image supervisor every isolated worker command is wrapped in."""

WORKER_PYTHON = "python3"
BUNDLE_FILE_NAME = "branch.bundle"
"""Where, under the result mount, the worker writes the bundle of its branch."""

_CONTAINER_NAME_PREFIX = "enginery-"
_NAME_DIGEST_CHARS = 24
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NO_NEW_PRIVILEGES = re.compile(r"^no-new-privileges[:=]true$")
_NANOS = 1_000_000_000


def container_name_for(operation_id: str) -> str:
    """The deterministic, engine-legal container name of one operation's worker."""
    if not operation_id.strip():
        raise InvalidInputError("operation id must be non-blank")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{_CONTAINER_NAME_PREFIX}{digest[:_NAME_DIGEST_CHARS]}"


def _cpus(nano_cpus: int) -> str:
    """Exact decimal text of a cpu count, which the engine converts back to ``nano_cpus``."""
    whole, fraction = divmod(nano_cpus, _NANOS)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:09d}".rstrip("0")


def hardening_arguments(profile: RunnerProfile, *, network: str) -> tuple[str, ...]:
    """The flags every worker and probe container carries, whatever it mounts or runs."""
    limits = profile.resources
    arguments = ["--user", f"{profile.uid}:{profile.gid}"]
    for capability in profile.cap_drop:
        arguments += ["--cap-drop", capability]
    for option in profile.security_options:
        arguments += ["--security-opt", option]
    arguments.append("--read-only")
    arguments += [
        "--tmpfs",
        f"{WORKER_TMP}:rw,nosuid,nodev,exec,size={limits.tmp_mib}m",
        "--tmpfs",
        (
            f"{WORKER_HOME}:rw,nosuid,nodev,exec,size={limits.home_mib}m,"
            f"uid={profile.uid},gid={profile.gid}"
        ),
        "--pids-limit",
        str(limits.pids),
        "--memory",
        f"{limits.memory_mib}m",
        "--memory-swap",
        f"{limits.memory_mib}m",
        "--cpus",
        _cpus(limits.nano_cpus),
        "--network",
        network,
        "--ipc",
        "private",
        "--init",
    ]
    return tuple(arguments)


def _plain_path(path: str, role: str) -> str:
    """A host path safe to embed in a ``-v`` value: no separators or control characters."""
    if ":" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise InvalidInputError(
            f"{role} path cannot be bind-mounted: it contains ':' or a control character"
        )
    return path


def _flag_value(value: str, flag: str) -> str:
    if not value or value.startswith("-"):
        raise InvalidInputError(f"worker argument {flag} needs a value that is not a flag")
    return value


def _environment_arguments(spec: LaunchSpec) -> tuple[str, ...]:
    allowed = set(spec.profile.env_allowlist)
    seen: set[str] = set()
    arguments: list[str] = []
    for key, value in spec.env:
        if key not in allowed:
            raise InvalidInputError(
                "environment key is outside the profile allowlist", details={"key": key}
            )
        if not _ENV_KEY.fullmatch(key) or key in seen:
            raise InvalidInputError(
                "environment key is malformed or repeated", details={"key": key}
            )
        if "\x00" in value:
            raise InvalidInputError("environment value contains NUL", details={"key": key})
        seen.add(key)
        arguments += ["-e", f"{key}={value}"]
    return tuple(arguments)


def worker_command(spec: LaunchSpec, inner_command: Sequence[str]) -> tuple[str, ...]:
    """The command run inside the container: the in-image supervisor, then the harness."""
    if not inner_command or any(not part for part in inner_command):
        raise InvalidInputError("the isolated worker needs a non-blank inner command")
    command = [
        WORKER_PYTHON,
        WORKER_ENTRY_PATH,
        "--operation-id",
        _flag_value(spec.operation_id, "--operation-id"),
        "--result",
        f"{OUT_MOUNT}/{_flag_value(spec.result_name, '--result')}",
        "--deadline-seconds",
        str(spec.time_budget_seconds),
    ]
    if spec.bundle_branch is not None and spec.bundle_base is not None:
        command += [
            "--bundle-branch",
            _flag_value(spec.bundle_branch, "--bundle-branch"),
            "--bundle-base",
            _flag_value(spec.bundle_base, "--bundle-base"),
            "--bundle-path",
            f"{OUT_MOUNT}/{BUNDLE_FILE_NAME}",
        ]
    for path, content in spec.seed_files:
        if (
            not path.startswith("/")
            or "=" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            raise InvalidInputError("seed file path must be absolute without '=' or controls")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        command += ["--seed-file", f"{path}={encoded}"]
    command += ["--", *inner_command]
    return tuple(command)


def create_arguments(spec: LaunchSpec, inner_command: Sequence[str]) -> tuple[str, ...]:
    """The complete ``docker create`` arguments for one isolated worker container.

    Excludes the ``docker`` executable and ``--context``. The environment is exactly
    ``spec.env``, re-checked here against the profile allowlist; nothing is inherited.
    """
    expected_name = container_name_for(spec.operation_id)
    if spec.container_name != expected_name:
        raise InvalidInputError(
            "launch spec container name is not the one derived from its operation id"
        )
    if spec.workspace_path == spec.out_path:
        raise InvalidInputError("the workspace and result mounts must be different directories")
    workspace = _plain_path(spec.workspace_path, "workspace")
    out = _plain_path(spec.out_path, "result")
    environment = _environment_arguments(spec)
    command = worker_command(spec, inner_command)
    return (
        "create",
        *hardening_arguments(spec.profile, network=spec.profile.network),
        "-v",
        f"{workspace}:{WORKSPACE_MOUNT}:rw",
        "-v",
        f"{out}:{OUT_MOUNT}:rw",
        "-w",
        WORKSPACE_MOUNT,
        "--label",
        f"{RUNNER_LABEL}=1",
        "--label",
        f"{OPERATION_LABEL}={spec.operation_id}",
        "--name",
        spec.container_name,
        *environment,
        spec.image,
        *command,
    )


def probe_arguments(
    profile: RunnerProfile, image: str, command: Sequence[str], *, name: str
) -> tuple[str, ...]:
    """``docker run`` arguments for a throw-away, network-less, mount-less probe container."""
    if not command or any(not part for part in command):
        raise InvalidInputError("a probe needs a non-blank command")
    home = ("-e", f"HOME={WORKER_HOME}") if "HOME" in profile.env_allowlist else ()
    return (
        "run",
        "--rm",
        *hardening_arguments(profile, network="none"),
        "--label",
        f"{PROBE_LABEL}=1",
        "--name",
        name,
        "-w",
        WORKER_HOME,
        *home,
        image,
        *command,
    )


# --- what the engine actually created ------------------------------------------------


def _map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _security_options(value: object) -> tuple[str, ...]:
    return tuple(
        "no-new-privileges" if _NO_NEW_PRIVILEGES.fullmatch(option) else option
        for option in _texts(value)
    )


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _mount_entries(container: Mapping[str, object]) -> tuple[tuple[str, str, bool], ...]:
    """Every mount the engine reports, from each place it can appear, de-duplicated."""
    host = _map(container.get("HostConfig"))
    found: set[tuple[str, str, bool]] = set()
    for item in _items(container.get("Mounts")):
        entry = _map(item)
        destination = entry.get("Destination")
        if isinstance(destination, str):
            found.add((str(entry.get("Type", "")), destination, entry.get("RW") is not False))
    for item in _items(host.get("Mounts")):
        entry = _map(item)
        target = entry.get("Target")
        if isinstance(target, str):
            found.add((str(entry.get("Type", "")), target, entry.get("ReadOnly") is not True))
    for bind in _texts(host.get("Binds")):
        parts = bind.split(":")
        if len(parts) >= 2:
            bind_options = parts[2].split(",") if len(parts) >= 3 else []
            found.add(("bind", parts[1], "ro" not in bind_options))
    for destination, tmpfs_options in _map(host.get("Tmpfs")).items():
        flags = str(tmpfs_options).split(",")
        found.add(("tmpfs", destination, "ro" not in flags))
    return tuple(sorted(found))


def _devices(host: Mapping[str, object]) -> tuple[str, ...]:
    devices: list[str] = []
    for item in _items(host.get("Devices")):
        entry = _map(item)
        devices.append(f"{entry.get('PathOnHost', '')}:{entry.get('PathInContainer', '')}")
    return tuple(devices)


def _environment_keys(
    container: Mapping[str, object], image: Mapping[str, object] | None
) -> tuple[str, ...]:
    """Keys the container's environment has beyond what the image itself bakes in."""
    baked = set(_texts(_map(_map(image).get("Config")).get("Env"))) if image is not None else set()
    keys = {
        entry.partition("=")[0]
        for entry in _texts(_map(container.get("Config")).get("Env"))
        if entry not in baked
    }
    return tuple(sorted(keys))


def effective_from_inspect(
    container: Mapping[str, object], image: Mapping[str, object] | None = None
) -> EffectiveContainer:
    """Reduce one ``docker inspect`` document to the facts the profile is checked against.

    ``image`` is the inspect document of the image the container was created from; when
    given, environment entries the image itself defines are not counted as injected. Only
    environment *keys* are recorded, never values.
    """
    host = _map(container.get("HostConfig"))
    config = _map(container.get("Config"))
    settings = _map(container.get("NetworkSettings"))
    user = config.get("User")
    return EffectiveContainer(
        user=user if isinstance(user, str) else "",
        cap_drop=_texts(host.get("CapDrop")),
        cap_add=_texts(host.get("CapAdd")),
        security_options=_security_options(host.get("SecurityOpt")),
        privileged=host.get("Privileged") is True,
        read_only_rootfs=host.get("ReadonlyRootfs") is True,
        memory_bytes=_integer(host.get("Memory")),
        memory_swap_bytes=_integer(host.get("MemorySwap")),
        nano_cpus=_integer(host.get("NanoCpus")),
        pids_limit=_integer(host.get("PidsLimit")),
        network_mode=str(host.get("NetworkMode", "")),
        networks=tuple(sorted(_map(settings.get("Networks")))),
        mounts=_mount_entries(container),
        devices=_devices(host),
        env_keys=_environment_keys(container, image),
    )


def read_effective(engine: OciEngine, name: str) -> EffectiveContainer:
    """What the engine reports container ``name`` was created with."""
    container = engine.container_inspect(name)
    image_reference = container.get("Image")
    image = engine.image_inspect(image_reference) if isinstance(image_reference, str) else None
    return effective_from_inspect(container, image)


def launch_violations(spec: LaunchSpec, container: Mapping[str, object]) -> tuple[str, ...]:
    """Ways the created container departs from the launch beyond what ``EffectiveContainer``
    records: the wrong image, or an engine-level namespace, link, port or device grant."""
    host = _map(container.get("HostConfig"))
    problems: list[str] = []
    if container.get("Image") != spec.image:
        problems.append("container was not created from the pinned image id")
    if host.get("PidMode") not in ("", None):
        problems.append(f"container shares a pid namespace ({host.get('PidMode')!r})")
    if host.get("IpcMode") != "private":
        problems.append(f"ipc mode is {host.get('IpcMode')!r}, expected 'private'")
    if host.get("UTSMode") not in ("", None):
        problems.append("container shares the host uts namespace")
    if host.get("UsernsMode") not in ("", None):
        problems.append("container uses a non-default user namespace mode")
    for key, label in (
        ("VolumesFrom", "volumes from another container"),
        ("Links", "container links"),
        ("DeviceRequests", "device requests"),
        ("DeviceCgroupRules", "device cgroup rules"),
        ("PortBindings", "published ports"),
    ):
        if host.get(key):
            problems.append(f"container has {label}")
    if host.get("PublishAllPorts") is True:
        problems.append("container publishes all exposed ports")
    return tuple(problems)
