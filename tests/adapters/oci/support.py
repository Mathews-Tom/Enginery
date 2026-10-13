"""A stateful in-memory stand-in for the docker CLI, and builders shared by the OCI tests.

``FakeDocker`` is an ``EngineRunner``: it records every command line it receives and answers
from a small model of a container engine. ``create`` really parses the flags it is given and
``container inspect`` reports what those flags amount to, so a test that drops or weakens a
flag sees exactly the engine-reported weakness the launcher must refuse. No process is started.
"""

from __future__ import annotations

import copy
import json
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import container_name_for, create_arguments
from enginery.domain.runner import ImageIdentity, LaunchSpec, PermissionMapping, RunnerProfile
from enginery.engine.supervisor import HostIdentity

IMAGE_ID = "sha256:" + "a" * 64
RECIPE_DIGEST = "sha256:" + "b" * 64
IMAGE_ENV = ("PATH=/usr/local/bin:/usr/bin:/bin", "NODE_VERSION=22.11.0", "HOME=/home/worker")

DOCKER_DESKTOP_INFO: dict[str, Any] = {
    "ServerVersion": "29.8.2",
    "OperatingSystem": "Docker Desktop",
    "KernelVersion": "7.0.14-linuxkit",
    "CgroupVersion": "2",
    "CgroupDriver": "cgroupfs",
    "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"],
    "MemoryLimit": True,
    "SwapLimit": True,
    "CpuCfsPeriod": True,
    "CpuCfsQuota": True,
    "PidsLimit": True,
    "Warnings": None,
}

ROOTLESS_INFO: dict[str, Any] = {
    **DOCKER_DESKTOP_INFO,
    "ServerVersion": "29.9.0",
    "OperatingSystem": "Ubuntu 24.04.5 LTS",
    "KernelVersion": "6.8.0-142-generic",
    "CgroupDriver": "systemd",
    "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless", "name=cgroupns"],
}

HOST = HostIdentity(machine_id="machine-1", boot_id="boot-1", hostname="host.example")
PERMISSIONS = PermissionMapping(
    harness="omp",
    name="omp-isolated-test",
    arguments=("--approval", "ask"),
    unconditional_bypass=False,
    rationale="test mapping",
)
INNER = ("omp", "-p", "do the thing")


def image_identity(image_id: str = IMAGE_ID) -> ImageIdentity:
    return ImageIdentity(
        image_id=image_id, recipe_digest=RECIPE_DIGEST, harness_versions=(("omp", "18.10.3"),)
    )


def verify_image(_engine: OciEngine, image_id: str) -> ImageIdentity:
    """An injected image verifier that accepts the fake image."""
    return image_identity(image_id)


def host_probe() -> HostIdentity:
    return HOST


def make_spec(
    root: Path,
    *,
    operation_id: str = "op-1",
    env: tuple[tuple[str, str], ...] = (("DISABLE_TELEMETRY", "1"),),
    profile: RunnerProfile | None = None,
    **overrides: Any,
) -> LaunchSpec:
    """A launch spec over fresh workspace/out directories; the receipt lives outside both."""
    workspace = root / "workspace"
    out = root / "out"
    workspace.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    fields: dict[str, Any] = {
        "operation_id": operation_id,
        "container_name": container_name_for(operation_id),
        "image": IMAGE_ID,
        "profile": profile or RunnerProfile(),
        "harness": "omp",
        "workspace_path": str(workspace),
        "out_path": str(out),
        "receipt_path": str(root / "receipts" / f"{operation_id}.json"),
        "result_name": "result.json",
        "time_budget_seconds": 600,
        "permissions": PERMISSIONS,
        "env": env,
    }
    fields.update(overrides)
    return LaunchSpec(**fields)


class _FakeContainer:
    def __init__(self, inspect: dict[str, Any]) -> None:
        self.inspect = inspect
        self.state = "created"
        self.exit_code = 0
        self.exited = threading.Event()


_VALUE_FLAGS = frozenset(
    {
        "--user",
        "--cap-drop",
        "--cap-add",
        "--security-opt",
        "--tmpfs",
        "--pids-limit",
        "--memory",
        "--memory-swap",
        "--cpus",
        "--network",
        "--ipc",
        "-v",
        "-w",
        "--label",
        "--name",
        "-e",
        "--device",
        "--pid",
    }
)
_BOOLEAN_FLAGS = frozenset({"--read-only", "--init", "--privileged"})
_MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _memory_bytes(text: str) -> int:
    unit = text[-1].lower()
    if unit in _MEMORY_UNITS:
        return int(text[:-1]) * _MEMORY_UNITS[unit]
    return int(text)


class FakeDocker:
    """An ``EngineRunner`` modelling just enough of docker for the launcher and the adapters."""

    def __init__(self, *, info: Mapping[str, Any] | None = None) -> None:
        self.info: dict[str, Any] = copy.deepcopy(dict(info or DOCKER_DESKTOP_INFO))
        self.images: dict[str, dict[str, Any]] = {
            IMAGE_ID: {"Id": IMAGE_ID, "Config": {"Env": list(IMAGE_ENV), "Labels": {}}}
        }
        self.networks: dict[str, bool] = {}
        self.containers: dict[str, _FakeContainer] = {}
        self.calls: list[tuple[str, ...]] = []
        self.context: str | None = None
        self.exit_code = 0
        self.hold_running = False
        self.log_text = "worker log line\n"
        self.create_stderr = ""
        self.tamper: Callable[[dict[str, Any]], None] | None = None
        self.failures: dict[str, tuple[int, str]] = {}
        self.unreachable = False
        self.hooks: dict[str, Callable[[tuple[str, ...]], None]] = {}

    # --- test controls ----------------------------------------------------------------

    def engine(self, *, context: str | None = None) -> OciEngine:
        return OciEngine(context=context, runner=self)

    def create_worker(self, spec: LaunchSpec, inner: Sequence[str] = INNER) -> None:
        """Create ``spec``'s worker container exactly as the launcher would."""
        completed = self.engine().run_cli(create_arguments(spec, inner))
        assert completed.returncode == 0, completed.stderr

    def finish(self, name: str, exit_code: int) -> None:
        """Let a held container exit on its own with ``exit_code``."""
        container = self.containers[name]
        container.exit_code = exit_code
        container.state = "exited"
        container.exited.set()

    @property
    def verbs(self) -> list[str]:
        """Each command as ``verb`` or ``noun verb``, in the order it was issued."""
        return [
            " ".join(args[:2]) if args[0] in {"network", "image", "container"} else args[0]
            for args in self.calls
        ]

    def calls_of(self, verb: str) -> list[tuple[str, ...]]:
        return [args for args, name in zip(self.calls, self.verbs, strict=True) if name == verb]

    # --- EngineRunner -----------------------------------------------------------------

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        input: str | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        tokens = list(argv)
        assert tokens[0] == "docker", tokens
        position = 1
        if tokens[position] == "--context":
            self.context = tokens[position + 1]
            position += 2
        args = tuple(tokens[position:])
        self.calls.append(args)
        verb = self.verbs[-1]
        hook = self.hooks.get(verb)
        if hook is not None:
            hook(args)
        injected = self.failures.get(verb)
        if injected is not None:
            return self._done(argv, injected[0], stderr=injected[1])
        handler = getattr(self, f"_do_{verb.replace(' ', '_')}", None)
        assert handler is not None, f"fake docker does not model {verb!r}: {args}"
        return handler(args, argv)

    @staticmethod
    def _done(
        argv: Sequence[str], returncode: int = 0, *, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(tuple(argv), returncode, stdout, stderr)

    def _missing(
        self, argv: Sequence[str], kind: str, name: str
    ) -> subprocess.CompletedProcess[str]:
        if kind == "network":
            return self._done(
                argv,
                1,
                stdout="[]\n",
                stderr=f"Error response from daemon: network {name} not found",
            )
        return self._done(argv, 1, stderr=f"Error response from daemon: No such {kind}: {name}")

    # --- verbs ------------------------------------------------------------------------

    def _do_info(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        if self.unreachable:
            return self._done(
                argv,
                1,
                stdout=json.dumps({"ServerErrors": ["Cannot connect to the Docker daemon"]}),
            )
        return self._done(argv, stdout=json.dumps(self.info))

    def _do_network_inspect(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        name = args[2]
        if name not in self.networks:
            return self._missing(argv, "network", name)
        return self._done(
            argv, stdout=json.dumps([{"Name": name, "Internal": self.networks[name]}])
        )

    def _do_network_create(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        name = args[-1]
        if name in self.networks:
            return self._done(
                argv,
                1,
                stderr=f"Error response from daemon: network with name {name} already exists",
            )
        self.networks[name] = "--internal" in args
        return self._done(argv, stdout="networkid\n")

    def _do_image_inspect(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        reference = args[-1]
        image = self.images.get(reference)
        if image is None:
            return self._missing(argv, "image", reference)
        if "--format" in args:
            template = args[args.index("--format") + 1]
            assert template == '{{.Id}}{{"\\n"}}{{json .Config.Labels}}', template
            labels = json.dumps(image["Config"]["Labels"] or None)
            return self._done(argv, stdout=f"{image['Id']}\n{labels}\n")
        return self._done(argv, stdout=json.dumps([image]))

    def _do_container_inspect(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        container = self.containers.get(args[2])
        if container is None:
            return self._missing(argv, "container", args[2])
        document = copy.deepcopy(container.inspect)
        document["State"] = {"Status": container.state}
        if self.tamper is not None:
            self.tamper(document)
        return self._done(argv, stdout=json.dumps([document]))

    def _do_create(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        parsed = self._parse_create(args[1:])
        name = parsed["name"]
        if name in self.containers:
            return self._done(
                argv, 125, stderr=f'Conflict. The container name "/{name}" is already in use'
            )
        image = self.images.get(parsed["image"])
        if image is None:
            return self._done(argv, 125, stderr=f"Unable to find image '{parsed['image']}' locally")
        self.containers[name] = _FakeContainer(self._inspect_document(parsed, image))
        return self._done(argv, stdout=f"{'c' * 64}\n", stderr=self.create_stderr)

    def _do_start(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        container = self.containers.get(args[1])
        if container is None:
            return self._missing(argv, "container", args[1])
        container.state = "running"
        if not self.hold_running:
            container.exit_code = self.exit_code
            container.state = "exited"
            container.exited.set()
        return self._done(argv, stdout=f"{args[1]}\n")

    def _do_wait(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        container = self.containers.get(args[1])
        if container is None:
            return self._missing(argv, "container", args[1])
        container.exited.wait()
        return self._done(argv, stdout=f"{container.exit_code}\n")

    def _do_kill(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        container = self.containers.get(args[-1])
        if container is None:
            return self._missing(argv, "container", args[-1])
        if container.state != "running":
            return self._done(
                argv, 1, stderr=f"Error response from daemon: container {args[-1]} is not running"
            )
        container.exit_code = 137
        container.state = "exited"
        container.exited.set()
        return self._done(argv, stdout=f"{args[-1]}\n")

    def _do_rm(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        name = args[-1]
        container = self.containers.get(name)
        if container is None:
            return self._missing(argv, "container", name)
        if container.state == "running" and "--force" not in args:
            return self._done(argv, 1, stderr="cannot remove a running container")
        container.exit_code = 137 if container.state == "running" else container.exit_code
        container.exited.set()
        del self.containers[name]
        return self._done(argv, stdout=f"{name}\n")

    def _do_logs(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        if args[-1] not in self.containers:
            return self._missing(argv, "container", args[-1])
        return self._done(argv, stdout=self.log_text)

    def _do_ps(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        label = args[args.index("--filter") + 1].removeprefix("label=")
        key, _, value = label.partition("=")
        names = [
            name
            for name, container in self.containers.items()
            if container.inspect["Config"]["Labels"].get(key) == value
        ]
        return self._done(argv, stdout="".join(f"{name}\n" for name in names))

    def _do_run(
        self, args: tuple[str, ...], argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        # ``docker run --rm`` of a probe: the container is created, runs, and is removed.
        return self._done(argv, self.exit_code, stdout=self.log_text)

    # --- create parsing ---------------------------------------------------------------

    @staticmethod
    def _parse_create(tokens: Sequence[str]) -> dict[str, Any]:
        parsed: dict[str, Any] = {
            "cap_drop": [],
            "cap_add": [],
            "security_opt": [],
            "tmpfs": {},
            "binds": [],
            "labels": {},
            "env": [],
            "devices": [],
            "read_only": False,
            "init": False,
            "privileged": False,
        }
        position = 0
        while position < len(tokens):
            token = tokens[position]
            if token in _BOOLEAN_FLAGS:
                parsed[token.lstrip("-").replace("-", "_")] = True
                position += 1
            elif token in _VALUE_FLAGS:
                value = tokens[position + 1]
                position += 2
                if token == "--cap-drop":
                    parsed["cap_drop"].append(value)
                elif token == "--cap-add":
                    parsed["cap_add"].append(value)
                elif token == "--security-opt":
                    parsed["security_opt"].append(value)
                elif token == "--tmpfs":
                    destination, _, options = value.partition(":")
                    parsed["tmpfs"][destination] = options
                elif token == "-v":
                    parsed["binds"].append(value)
                elif token == "--label":
                    key, _, label_value = value.partition("=")
                    parsed["labels"][key] = label_value
                elif token == "-e":
                    parsed["env"].append(value)
                elif token == "--device":
                    parsed["devices"].append(value)
                else:
                    parsed[token.lstrip("-").replace("-", "_")] = value
            else:
                parsed["image"] = token
                parsed["command"] = list(tokens[position + 1 :])
                return parsed
        raise AssertionError(f"create has no image: {tokens}")

    @staticmethod
    def _inspect_document(parsed: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
        network = parsed.get("network", "bridge")
        binds = [bind if bind.count(":") >= 2 else f"{bind}:rw" for bind in parsed["binds"]]
        cpus = parsed.get("cpus")
        return {
            "Id": "c" * 64,
            "Name": f"/{parsed['name']}",
            "Image": image["Id"],
            "Config": {
                "User": parsed.get("user", ""),
                "Env": [*parsed["env"], *image["Config"]["Env"]],
                "Labels": dict(parsed["labels"]),
                "Cmd": parsed["command"],
                "WorkingDir": parsed.get("w", ""),
            },
            "HostConfig": {
                "CapDrop": parsed["cap_drop"] or None,
                "CapAdd": parsed["cap_add"] or None,
                "SecurityOpt": parsed["security_opt"] or None,
                "Privileged": parsed["privileged"],
                "ReadonlyRootfs": parsed["read_only"],
                "Memory": _memory_bytes(parsed["memory"]) if "memory" in parsed else 0,
                "MemorySwap": _memory_bytes(parsed["memory_swap"])
                if "memory_swap" in parsed
                else 0,
                "NanoCpus": int(Decimal(cpus) * 1_000_000_000) if cpus else 0,
                "PidsLimit": int(parsed["pids_limit"]) if "pids_limit" in parsed else None,
                "NetworkMode": network,
                "Tmpfs": dict(parsed["tmpfs"]),
                "Binds": binds,
                "Devices": [
                    {"PathOnHost": device.split(":")[0], "PathInContainer": device.split(":")[-1]}
                    for device in parsed["devices"]
                ],
                "IpcMode": parsed.get("ipc", "private"),
                "PidMode": parsed.get("pid", ""),
                "UTSMode": "",
                "UsernsMode": "",
                "VolumesFrom": None,
                "Links": None,
                "DeviceRequests": None,
                "DeviceCgroupRules": None,
                "PortBindings": {},
                "PublishAllPorts": False,
                "Init": parsed["init"],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": bind.split(":")[0],
                    "Destination": bind.split(":")[1],
                    "Mode": bind.split(":")[2],
                    "RW": bind.split(":")[2] == "rw",
                }
                for bind in binds
            ],
            "NetworkSettings": {"Networks": {network: {"NetworkID": ""}}},
        }
