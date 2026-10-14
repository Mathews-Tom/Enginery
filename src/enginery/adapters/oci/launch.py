"""Build the hardened container command lines and read back what the engine created.

``create_arguments`` is the only place the isolated worker's ``docker create`` command line
is assembled, and ``probe_arguments`` shares its hardening. ``effective_from_inspect`` turns
the engine's own report of the created container into the record the profile is checked
against, so a launch is judged by what the engine holds, not by what was requested.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from enginery.adapters.oci.engine import (
    LAUNCHER_LABEL,
    OPERATION_LABEL,
    PROBE_LABEL,
    RUNNER_LABEL,
    OciEngine,
)
from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    DISK_BOUND_KERNEL_VOLUME,
    OUT_MOUNT,
    WORKER_HOME,
    WORKER_TMP,
    WORKSPACE_MOUNT,
    EffectiveContainer,
    LaunchSpec,
    ResourceBounds,
    RunnerProfile,
)
from enginery.engine.supervisor import ProcessIdentity

WORKER_ENTRY_PATH = "/opt/enginery/worker_entry.py"
"""The in-image supervisor every isolated worker command is wrapped in."""

WORKER_PYTHON = "python3"
BUNDLE_FILE_NAME = "branch.bundle"
"""Where, under the result mount, the worker writes the bundle of its branch."""

_CONTAINER_NAME_PREFIX = "enginery-"
_NAME_DIGEST_CHARS = 24
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NO_NEW_PRIVILEGES = re.compile(r"^no-new-privileges[:=]true$")
_NONCE = re.compile(r"^[A-Za-z0-9]{16,128}$")
_NANOS = 1_000_000_000

LOG_DRIVER = "json-file"
LOG_MAX_SIZE = "1m"
LOG_MAX_FILE = 2
"""The container log is rotated and capped at ``LOG_MAX_FILE`` files of ``LOG_MAX_SIZE``."""
STORE_HOLDER_COMMAND = ("sleep", "infinity")
"""What the store container runs: nothing, so the tmpfs volumes stay mounted while it lives."""
_MIB = 1024 * 1024


def container_name_for(operation_id: str) -> str:
    """The deterministic, engine-legal container name of one operation's worker."""
    if not operation_id.strip():
        raise InvalidInputError("operation id must be non-blank")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{_CONTAINER_NAME_PREFIX}{digest[:_NAME_DIGEST_CHARS]}"


def worker_container_gone(engine: OciEngine, operation_id: str) -> bool:
    """Whether an operation's worker container no longer exists, running or not.

    The one container lookup shared by recovery's proof that a worker is gone and by the
    operator's drain, so both ask the engine about exactly the same name. An engine that
    cannot be reached raises rather than answering.
    """
    return not engine.container_exists(container_name_for(operation_id))


def _cpus(nano_cpus: int) -> str:
    """Exact decimal text of a cpu count, which the engine converts back to ``nano_cpus``."""
    whole, fraction = divmod(nano_cpus, _NANOS)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:09d}".rstrip("0")


def max_file_bytes(profile: RunnerProfile) -> int:
    """The largest single file a worker process may write (``RLIMIT_FSIZE``).

    A per-file kernel limit on top of the volumes' own size caps.
    """
    limits = profile.resources
    return min(limits.workspace_mib, limits.out_mib) * _MIB


def resource_bounds(profile: RunnerProfile) -> ResourceBounds:
    """The disk and log bounds a launch runs under, as recorded in its receipt."""
    limits = profile.resources
    return ResourceBounds(
        disk_mechanism=DISK_BOUND_KERNEL_VOLUME,
        workspace_bytes=limits.workspace_mib * _MIB,
        out_bytes=limits.out_mib * _MIB,
        workspace_inodes=limits.workspace_inodes,
        out_inodes=limits.out_inodes,
        max_file_bytes=max_file_bytes(profile),
        log_driver=LOG_DRIVER,
        log_max_size=LOG_MAX_SIZE,
        log_max_file=LOG_MAX_FILE,
    )


ENGINE_MEMORY_HEADROOM_MIB = 1024
"""Memory the engine's machine keeps for itself beyond one worker: the engine daemon, the store
container, the relay and the operating system of a Docker Desktop or Lima virtual machine."""


def engine_memory_floor_mib(profile: RunnerProfile) -> int:
    """The least engine memory one attempt of ``profile`` needs besides its seeded workspace.

    A worker may use its whole memory limit, and everything it writes into a volume, ``/tmp`` or
    its home directory is charged to that limit. The pages the host seeds into the workspace
    volume are not: the engine writes them and no container is charged for them (measured on
    Docker Desktop: a worker limited to 64 MiB beside 200 MiB of seeded content was killed after
    writing 58 MiB, and the virtual machine held both). One attempt therefore needs, in the
    engine's memory, the seeded content plus this floor.
    """
    return profile.resources.memory_mib + ENGINE_MEMORY_HEADROOM_MIB


def engine_memory_shortfall(profile: RunnerProfile, memory_total_bytes: int) -> str | None:
    """Why an engine with ``memory_total_bytes`` cannot hold one attempt, or ``None`` if it can."""
    floor = engine_memory_floor_mib(profile)
    if memory_total_bytes >= floor * _MIB:
        return None
    return (
        f"the engine has {memory_total_bytes // _MIB} MiB of memory and one worker needs at "
        f"least {floor} MiB ({profile.resources.memory_mib} MiB worker limit plus "
        f"{ENGINE_MEMORY_HEADROOM_MIB} MiB engine headroom) before any workspace is seeded; "
        "raise the memory of the Docker Desktop or Lima virtual machine"
    )


def seed_byte_ceiling(profile: RunnerProfile, memory_total_bytes: int) -> int:
    """The most workspace content one attempt may seed: the volume's cap, or what the engine's
    memory leaves after the worker's floor if that is less (never negative)."""
    room = memory_total_bytes - engine_memory_floor_mib(profile) * _MIB
    return max(0, min(profile.resources.workspace_mib * _MIB, room))


@dataclass(frozen=True, slots=True)
class StoreNames:
    """The names of one attempt's store container and its two capped volumes."""

    holder: str
    workspace_volume: str
    out_volume: str


def store_names_for(container_name: str) -> StoreNames:
    """The store container and volume names that belong to the worker ``container_name``."""
    return StoreNames(
        holder=f"{container_name}-store",
        workspace_volume=f"{container_name}-workspace",
        out_volume=f"{container_name}-out",
    )


VOLUME_MOUNT_FLAGS: dict[str, tuple[str, ...]] = {
    # The workspace is nosuid,nodev but NOT noexec: the worker runs the project's own
    # validation commands, test runners, scripts and compiled extensions from it, and a noexec
    # mount also refuses the executable mappings of shared libraries, so noexec would break the
    # workflow. A worker-written setuid file or device node there is inert (nosuid, nodev).
    "workspace": ("nosuid", "nodev"),
    # The result volume only ever holds the result file and the branch bundle, which the host
    # reads back as data; nothing in it is executed, so it is noexec as well.
    "out": ("nosuid", "nodev", "noexec"),
}


def volume_options(profile: RunnerProfile, role: str) -> dict[str, str]:
    """The local-driver options that make a volume a tmpfs capped in size and inodes.

    ``role`` is ``"workspace"`` or ``"out"``. The tmpfs root is owned by the worker and closed
    to everyone else, and the mount carries the flags of ``VOLUME_MOUNT_FLAGS[role]``.
    """
    limits = profile.resources
    size, inodes = (
        (limits.workspace_mib, limits.workspace_inodes)
        if role == "workspace"
        else (limits.out_mib, limits.out_inodes)
    )
    flags = ",".join(VOLUME_MOUNT_FLAGS[role])
    return {
        "type": "tmpfs",
        "device": "tmpfs",
        "o": (
            f"size={size}m,nr_inodes={inodes},uid={profile.uid},gid={profile.gid},mode=0700,{flags}"
        ),
    }


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
        "--cgroupns",
        "private",
        "--log-driver",
        LOG_DRIVER,
        "--log-opt",
        f"max-size={LOG_MAX_SIZE}",
        "--log-opt",
        f"max-file={LOG_MAX_FILE}",
        "--ulimit",
        f"fsize={max_file_bytes(profile)}",
    ]
    return tuple(arguments)


def _csv_field(text: str) -> str:
    """``text`` as one field of a ``--mount`` value, quoted when it holds a comma or quote."""
    if "," not in text and '"' not in text:
        return text
    return '"' + text.replace('"', '""') + '"'


def _creating_mount(spec: LaunchSpec, role: str, volume: str, destination: str) -> str:
    """The ``--mount`` value that creates one capped tmpfs volume (the store container's)."""
    fields = [
        "type=volume",
        f"src={volume}",
        f"dst={destination}",
        "volume-nocopy",
        "volume-driver=local",
        f"volume-label={RUNNER_LABEL}=1",
        *(
            _csv_field(f"volume-opt={key}={value}")
            for key, value in volume_options(spec.profile, role).items()
        ),
    ]
    return ",".join(fields)


def _joining_mount(volume: str, destination: str) -> str:
    """The ``--mount`` value that attaches a worker to a volume the store container created."""
    return f"type=volume,src={volume},dst={destination},volume-nocopy"


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


def _nonce_value(nonce: str) -> str:
    if not _NONCE.fullmatch(nonce):
        raise InvalidInputError("the attempt nonce must be 16 to 128 letters or digits")
    return nonce


def worker_command(
    spec: LaunchSpec, inner_command: Sequence[str], *, nonce: str
) -> tuple[str, ...]:
    """The command run inside the container: the in-image supervisor, then the harness.

    ``nonce`` is the launcher's per-attempt value; the supervisor echoes it in its result.
    """
    if not inner_command or any(not part for part in inner_command):
        raise InvalidInputError("the isolated worker needs a non-blank inner command")
    command = [
        WORKER_PYTHON,
        WORKER_ENTRY_PATH,
        "--operation-id",
        _flag_value(spec.operation_id, "--operation-id"),
        "--nonce",
        _nonce_value(nonce),
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


def launcher_label(launcher: ProcessIdentity) -> str:
    """The label value that names the launcher process a container belongs to."""
    host = launcher.host
    return json.dumps(
        {
            "pid": launcher.pid,
            "pgid": launcher.process_group_id,
            "start": launcher.start_identity,
            "machine": "" if host is None else host.machine_id,
            "boot": "" if host is None else host.boot_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def create_arguments(
    spec: LaunchSpec,
    inner_command: Sequence[str],
    *,
    nonce: str,
    launcher: ProcessIdentity | None = None,
) -> tuple[str, ...]:
    """The complete ``docker create`` arguments for one isolated worker container.

    Excludes the ``docker`` executable and ``--context``. The environment is exactly
    ``spec.env``, re-checked here against the profile allowlist; nothing is inherited. When
    ``launcher`` is given, the container is labelled with that process so a later reconcile can
    tell a live attempt from one whose launcher died. The container joins exactly
    ``spec.attempt_network`` (its own internal network, or ``none``), never a shared one.
    """
    expected_name = container_name_for(spec.operation_id)
    if spec.container_name != expected_name:
        raise InvalidInputError(
            "launch spec container name is not the one derived from its operation id"
        )
    if spec.workspace_path == spec.out_path:
        raise InvalidInputError("the workspace and result mounts must be different directories")
    names = store_names_for(spec.container_name)
    environment = _environment_arguments(spec)
    command = worker_command(spec, inner_command, nonce=nonce)
    launcher_labels = (
        ("--label", f"{LAUNCHER_LABEL}={launcher_label(launcher)}") if launcher else ()
    )
    return (
        "create",
        *hardening_arguments(spec.profile, network=spec.attempt_network),
        "--mount",
        _joining_mount(names.workspace_volume, WORKSPACE_MOUNT),
        "--mount",
        _joining_mount(names.out_volume, OUT_MOUNT),
        "-w",
        WORKSPACE_MOUNT,
        "--label",
        f"{RUNNER_LABEL}=1",
        "--label",
        f"{OPERATION_LABEL}={spec.operation_id}",
        *launcher_labels,
        "--name",
        spec.container_name,
        *environment,
        spec.image,
        *command,
    )


def store_arguments(
    spec: LaunchSpec, *, launcher: ProcessIdentity | None = None
) -> tuple[str, ...]:
    """The ``docker create`` arguments of the store container of one attempt.

    The store container has the worker's hardening, no network and nothing to run: it only keeps
    the attempt's two capped tmpfs volumes mounted (they are gone while no container has them),
    and it creates them, so no volume ever exists without a container that refers to it. The
    host seeds the workspace through it before the worker starts and reads the result and the
    workspace back through it after the worker exits.
    """
    if spec.container_name != container_name_for(spec.operation_id):
        raise InvalidInputError(
            "launch spec container name is not the one derived from its operation id"
        )
    names = store_names_for(spec.container_name)
    launcher_labels = (
        ("--label", f"{LAUNCHER_LABEL}={launcher_label(launcher)}") if launcher else ()
    )
    return (
        "create",
        *hardening_arguments(spec.profile, network="none"),
        "--mount",
        _creating_mount(spec, "workspace", names.workspace_volume, WORKSPACE_MOUNT),
        "--mount",
        _creating_mount(spec, "out", names.out_volume, OUT_MOUNT),
        "-w",
        WORKSPACE_MOUNT,
        "--label",
        f"{RUNNER_LABEL}=1",
        "--label",
        f"{OPERATION_LABEL}={spec.operation_id}",
        *launcher_labels,
        "--name",
        names.holder,
        spec.image,
        *STORE_HOLDER_COMMAND,
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


_REQUIRED_MASKED_PATHS = frozenset(
    {"/proc/kcore", "/proc/keys", "/proc/sched_debug", "/sys/firmware"}
)
_REQUIRED_READONLY_PATHS = frozenset({"/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys"})
_TMPFS_UNITS = {"": 1, "k": 1024, "m": _MIB, "g": 1024 * _MIB}
_TMPFS_SIZE = re.compile(r"^size=(\d+)([kmg]?)$", re.IGNORECASE)


def _tmpfs_options(host: Mapping[str, object], destination: str) -> tuple[str, ...] | None:
    options = _map(host.get("Tmpfs")).get(destination)
    if not isinstance(options, str):
        return None
    return tuple(option for option in options.split(",") if option)


def _tmpfs_size_bytes(options: Sequence[str]) -> int | None:
    for option in options:
        found = _TMPFS_SIZE.fullmatch(option)
        if found is not None:
            return int(found.group(1)) * _TMPFS_UNITS[found.group(2).lower()]
    return None


def _scratch_violations(
    host: Mapping[str, object], destination: str, *, size_mib: int, owner: str | None
) -> list[str]:
    options = _tmpfs_options(host, destination)
    if options is None:
        return [f"scratch space {destination} is not a tmpfs"]
    problems: list[str] = []
    if _tmpfs_size_bytes(options) != size_mib * _MIB:
        problems.append(f"scratch space {destination} is not capped at {size_mib} MiB")
    for required in ("nosuid", "nodev"):
        if required not in options:
            problems.append(f"scratch space {destination} is not mounted {required}")
    if owner is not None and owner not in ",".join(options):
        problems.append(f"scratch space {destination} is not owned by the worker ({owner})")
    return problems


def _paths(value: object) -> frozenset[str]:
    return frozenset(_texts(value))


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _log_violations(host: Mapping[str, object]) -> list[str]:
    config = _map(host.get("LogConfig"))
    options = _map(config.get("Config"))
    problems: list[str] = []
    if config.get("Type") != LOG_DRIVER:
        problems.append(f"log driver is {config.get('Type')!r}, expected {LOG_DRIVER!r}")
    if options.get("max-size") != LOG_MAX_SIZE or _decimal(options.get("max-file")) != LOG_MAX_FILE:
        problems.append("container log is not rotated at the profile's size and file count")
    return problems


def _ulimit_violations(host: Mapping[str, object], profile: RunnerProfile) -> list[str]:
    expected = max_file_bytes(profile)
    fsize = [
        _map(item) for item in _items(host.get("Ulimits")) if _map(item).get("Name") == "fsize"
    ]
    if len(fsize) != 1 or fsize[0].get("Soft") != expected or fsize[0].get("Hard") != expected:
        return [f"the per-file size limit is not {expected} bytes"]
    return []


def launch_violations(spec: LaunchSpec, container: Mapping[str, object]) -> tuple[str, ...]:
    """Ways the created container departs from the launch beyond what ``EffectiveContainer``
    records.

    Checked from the engine's own report: the wrong image; a shared pid, ipc, uts, user or
    cgroup namespace; links, volumes from another container, device requests or rules,
    published ports; sysctls; a masked- or read-only-path set that is empty or missing the
    core entries (``systempaths=unconfined``); a runtime other than ``runc``; tmpfs scratch
    space that is not the profile's size (or is not nosuid/nodev, or the home tmpfs not owned by
    the worker); and a container log or per-file size limit that is not the profile's.
    """
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
    if host.get("CgroupnsMode") != "private":
        problems.append(f"cgroup namespace is {host.get('CgroupnsMode')!r}, expected 'private'")
    for key, label in (
        ("VolumesFrom", "volumes from another container"),
        ("Links", "container links"),
        ("DeviceRequests", "device requests"),
        ("DeviceCgroupRules", "device cgroup rules"),
        ("PortBindings", "published ports"),
        ("Sysctls", "sysctls"),
        ("ExtraHosts", "extra host entries"),
    ):
        if host.get(key):
            problems.append(f"container has {label}")
    if host.get("PublishAllPorts") is True:
        problems.append("container publishes all exposed ports")
    if not _paths(host.get("MaskedPaths")) >= _REQUIRED_MASKED_PATHS:
        problems.append("kernel paths are not masked (masked paths missing or emptied)")
    if not _paths(host.get("ReadonlyPaths")) >= _REQUIRED_READONLY_PATHS:
        problems.append("kernel paths are not read-only (read-only paths missing or emptied)")
    if host.get("Runtime") != "runc":
        problems.append(f"container runtime is {host.get('Runtime')!r}, expected 'runc'")
    resources = spec.profile.resources
    problems += _scratch_violations(host, WORKER_TMP, size_mib=resources.tmp_mib, owner=None)
    problems += _scratch_violations(
        host,
        WORKER_HOME,
        size_mib=resources.home_mib,
        owner=f"uid={spec.profile.uid}",
    )
    problems += _log_violations(host)
    problems += _ulimit_violations(host, spec.profile)
    return tuple(problems)


def _split_volume_options(
    options: Mapping[str, object],
) -> tuple[dict[str, object], list[str], list[str]]:
    """``options`` as the keys other than ``o``, its ``key=value`` settings and its mount flags.

    Settings and flags are sorted, so the comparison ignores order but sees an added or repeated
    token (``suid`` or ``exec`` after ``nosuid``, which would weaken the mount).
    """
    others = {key: value for key, value in options.items() if key != "o"}
    tokens = [token for token in str(options.get("o", "")).split(",") if token]
    settings = sorted(token for token in tokens if "=" in token)
    flags = sorted(token for token in tokens if "=" not in token)
    return others, settings, flags


def store_violations(
    spec: LaunchSpec,
    container: Mapping[str, object],
    volumes: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    """Ways a container's workspace and result mounts are not the attempt's capped volumes.

    ``volumes`` maps a volume name to the engine's inspect document of it. The caps are checked
    from what the engine holds: the local driver, a tmpfs of exactly the profile's size and
    inode count owned by the worker, the mount flags of ``VOLUME_MOUNT_FLAGS`` (nosuid and nodev
    on both, noexec as well on the result volume, with no flag added or repeated), and the
    runner label.
    """
    names = store_names_for(spec.container_name)
    mounts = {
        entry.get("Destination"): entry
        for entry in (_map(item) for item in _items(container.get("Mounts")))
    }
    problems: list[str] = []
    for role, volume, destination in (
        ("workspace", names.workspace_volume, WORKSPACE_MOUNT),
        ("result", names.out_volume, OUT_MOUNT),
    ):
        mount = mounts.get(destination, {})
        if mount.get("Type") != "volume" or mount.get("Name") != volume or mount.get("RW") is False:
            problems.append(f"the {role} mount is not the attempt's volume {volume}")
        document = volumes.get(volume, {})
        if document.get("Driver") != "local":
            problems.append(f"the {role} volume does not use the local driver")
        volume_role = "out" if role == "result" else "workspace"
        actual = _split_volume_options(_map(document.get("Options")))
        expected = _split_volume_options(volume_options(spec.profile, volume_role))
        if actual[:2] != expected[:2]:
            problems.append(
                f"the {role} volume is not a tmpfs capped at the profile's size and inode count"
            )
        if actual[2] != expected[2]:
            problems.append(
                f"the {role} volume is not mounted with exactly the flags "
                f"{','.join(VOLUME_MOUNT_FLAGS[volume_role])}"
            )
        if _map(document.get("Labels")).get(RUNNER_LABEL) != "1":
            problems.append(f"the {role} volume does not carry the runner label")
    return tuple(problems)
