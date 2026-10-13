from __future__ import annotations

import base64
import copy
import dataclasses
import itertools
import json
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import (
    container_name_for,
    create_arguments,
    effective_from_inspect,
    launch_violations,
    read_effective,
    worker_command,
)
from enginery.adapters.oci.launcher import make_tree_writable
from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    DEFAULT_ENV_ALLOWLIST,
    LaunchSpec,
    ResourceLimits,
    RunnerProfile,
    profile_violations,
)

from .support import IMAGE_ID, INNER, NONCE, FakeDocker, make_spec

# --- names ---------------------------------------------------------------------------


def test_container_name_is_deterministic_and_legal_for_the_engine() -> None:
    name = container_name_for("01J9ZX-operation/with:odd chars")

    assert name == container_name_for("01J9ZX-operation/with:odd chars")
    assert name.startswith("enginery-")
    assert len(name) == len("enginery-") + 24
    assert all(char in "0123456789abcdef" for char in name.removeprefix("enginery-"))
    assert container_name_for("op-a") != container_name_for("op-b")


def test_container_name_requires_an_operation_id() -> None:
    with pytest.raises(InvalidInputError):
        container_name_for("   ")


# --- the create command line ---------------------------------------------------------


def test_create_arguments_is_exactly_the_hardened_command_line(tmp_path: Path) -> None:
    spec = make_spec(
        tmp_path,
        env=(("HOME", "/home/worker"), ("ANTHROPIC_BASE_URL", "http://relay.internal:8080")),
    )

    arguments = create_arguments(spec, INNER, nonce=NONCE)

    assert arguments == (
        "create",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,exec,size=512m",
        "--tmpfs",
        "/home/worker:rw,nosuid,nodev,exec,size=512m,uid=65532,gid=65532",
        "--pids-limit",
        "512",
        "--memory",
        "4096m",
        "--memory-swap",
        "4096m",
        "--cpus",
        "2",
        "--network",
        "enginery-runner-internal",
        "--ipc",
        "private",
        "--init",
        "--cgroupns",
        "private",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--ulimit",
        "fsize=1073741824",
        "-v",
        f"{tmp_path / 'workspace'}:/workspace:rw",
        "-v",
        f"{tmp_path / 'out'}:/out:rw",
        "-w",
        "/workspace",
        "--label",
        "dev.enginery.runner=1",
        "--label",
        "dev.enginery.operation=op-1",
        "--name",
        container_name_for("op-1"),
        "-e",
        "HOME=/home/worker",
        "-e",
        "ANTHROPIC_BASE_URL=http://relay.internal:8080",
        IMAGE_ID,
        "python3",
        "/opt/enginery/worker_entry.py",
        "--operation-id",
        "op-1",
        "--nonce",
        NONCE,
        "--result",
        "/out/result.json",
        "--deadline-seconds",
        "600",
        "--",
        *INNER,
    )


def _values_of(arguments: Sequence[str], flag: str) -> list[str]:
    """The value of every occurrence of ``flag`` before the image (the container command is
    excluded, so a harness flag such as ``-p`` is not mistaken for an engine flag)."""
    engine_part = arguments[: arguments.index(IMAGE_ID)]
    return [value for key, value in itertools.pairwise(engine_part) if key == flag]


def test_create_arguments_never_grant_what_the_profile_forbids(tmp_path: Path) -> None:
    arguments = create_arguments(make_spec(tmp_path), INNER, nonce=NONCE)
    engine_part = arguments[: arguments.index(IMAGE_ID)]

    for forbidden in (
        "--privileged",
        "--cap-add",
        "--device",
        "--pid",
        "--env-file",
        "--volumes-from",
        "--publish",
        "-p",
        "--add-host",
        "--net",
    ):
        assert forbidden not in engine_part
    assert _values_of(arguments, "-v") == [
        f"{tmp_path / 'workspace'}:/workspace:rw",
        f"{tmp_path / 'out'}:/out:rw",
    ]
    assert "/var/run/docker.sock" not in " ".join(arguments)


def test_create_arguments_follow_the_profile_limits(tmp_path: Path) -> None:
    profile = RunnerProfile(
        uid=70000,
        gid=70001,
        network="relay-internal",
        resources=ResourceLimits(
            memory_mib=1536, cpus=0.5, pids=64, tmp_mib=128, home_mib=256, stop_grace_seconds=4
        ),
    )

    arguments = create_arguments(make_spec(tmp_path, profile=profile, env=()), INNER, nonce=NONCE)

    def after(flag: str) -> str:
        return arguments[arguments.index(flag) + 1]

    assert after("--user") == "70000:70001"
    assert after("--memory") == after("--memory-swap") == "1536m"
    assert after("--cpus") == "0.5"
    assert after("--pids-limit") == "64"
    assert after("--network") == "relay-internal"
    tmpfs = _values_of(arguments, "--tmpfs")
    assert tmpfs == [
        "/tmp:rw,nosuid,nodev,exec,size=128m",
        "/home/worker:rw,nosuid,nodev,exec,size=256m,uid=70000,gid=70001",
    ]
    assert "-e" not in arguments


def test_cpu_counts_are_written_so_the_engine_reads_back_the_profile_value(
    tmp_path: Path,
) -> None:
    for cpus, text in ((2.0, "2"), (1.5, "1.5"), (0.333333333, "0.333333333"), (0.1, "0.1")):
        profile = RunnerProfile(resources=ResourceLimits(cpus=cpus))
        arguments = create_arguments(
            make_spec(tmp_path, profile=profile, env=()), INNER, nonce=NONCE
        )
        assert arguments[arguments.index("--cpus") + 1] == text


def test_worker_command_carries_bundle_seed_files_and_the_inner_command(tmp_path: Path) -> None:
    spec = make_spec(
        tmp_path,
        bundle_branch="enginery/task-7",
        bundle_base="9fceb02d0ae598e95dc970b74767f19372d61af8",
        seed_files=(("/home/worker/.claude/settings.json", '{"note": "sémantique ✓"}'),),
        result_name="omp-result.json",
        time_budget_seconds=90,
    )

    command = worker_command(spec, ("claude", "-p", "--", "weird"), nonce=NONCE)

    assert command[:2] == ("python3", "/opt/enginery/worker_entry.py")
    separator = command.index("--")
    assert command[separator + 1 :] == ("claude", "-p", "--", "weird")
    wrapper = command[2:separator]
    assert wrapper[wrapper.index("--result") + 1] == "/out/omp-result.json"
    assert wrapper[wrapper.index("--deadline-seconds") + 1] == "90"
    assert wrapper[wrapper.index("--bundle-branch") + 1] == "enginery/task-7"
    assert wrapper[wrapper.index("--bundle-base") + 1] == spec.bundle_base
    assert wrapper[wrapper.index("--bundle-path") + 1] == "/out/branch.bundle"
    seed = wrapper[wrapper.index("--seed-file") + 1]
    path, _, encoded = seed.partition("=")
    assert path == "/home/worker/.claude/settings.json"
    assert base64.b64decode(encoded).decode("utf-8") == '{"note": "sémantique ✓"}'


def test_worker_command_without_a_bundle_asks_for_none(tmp_path: Path) -> None:
    command = worker_command(make_spec(tmp_path), INNER, nonce=NONCE)

    assert "--bundle-branch" not in command
    assert "--seed-file" not in command


# --- refusals ------------------------------------------------------------------------


def test_create_arguments_reassert_the_environment_allowlist(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    secret = "ghp_" + "x" * 36
    # The spec validates its own env; a spec mutated afterwards must still not get through.
    object.__setattr__(spec, "env", (("HOME", "/home/worker"), ("GH_TOKEN", secret)))

    with pytest.raises(InvalidInputError, match="allowlist") as raised:
        create_arguments(spec, INNER, nonce=NONCE)

    assert raised.value.details == {"key": "GH_TOKEN"}
    assert secret not in str(raised.value)


@pytest.mark.parametrize("key", ["PATH", "HOME", "ANTHROPIC_AUTH_TOKEN", "PI_CONFIG_DIR"])
def test_every_allowlisted_key_is_accepted(tmp_path: Path, key: str) -> None:
    assert key in DEFAULT_ENV_ALLOWLIST

    arguments = create_arguments(make_spec(tmp_path, env=((key, "value"),)), INNER, nonce=NONCE)

    assert ("-e", f"{key}=value") == arguments[arguments.index("-e") : arguments.index("-e") + 2]


def test_create_arguments_refuse_a_repeated_environment_key(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    object.__setattr__(spec, "env", (("HOME", "/a"), ("HOME", "/b")))

    with pytest.raises(InvalidInputError, match="repeated"):
        create_arguments(spec, INNER, nonce=NONCE)


def test_create_arguments_refuse_a_container_name_not_derived_from_the_operation(
    tmp_path: Path,
) -> None:
    spec = make_spec(tmp_path, container_name="enginery-" + "0" * 24)

    with pytest.raises(InvalidInputError, match="container name"):
        create_arguments(spec, INNER, nonce=NONCE)


def test_create_arguments_refuse_identical_workspace_and_result_directories(
    tmp_path: Path,
) -> None:
    spec = make_spec(tmp_path, out_path=str(tmp_path / "workspace"))

    with pytest.raises(InvalidInputError, match="different directories"):
        create_arguments(spec, INNER, nonce=NONCE)


@pytest.mark.parametrize("bad", ["/tmp/a:b", "/tmp/with\nnewline", "/tmp/tab\there"])
def test_create_arguments_refuse_host_paths_that_could_rewrite_a_mount(
    tmp_path: Path, bad: str
) -> None:
    spec = make_spec(tmp_path, workspace_path=bad)

    with pytest.raises(InvalidInputError, match="cannot be bind-mounted"):
        create_arguments(spec, INNER, nonce=NONCE)


@pytest.mark.parametrize("inner", [(), ("",), ("omp", "")])
def test_create_arguments_refuse_a_blank_inner_command(
    tmp_path: Path, inner: Sequence[str]
) -> None:
    with pytest.raises(InvalidInputError, match="inner command"):
        create_arguments(make_spec(tmp_path), inner, nonce=NONCE)


@pytest.mark.parametrize(
    "seed",
    [("relative/path", "x"), ("/abs/with=equals", "x"), ("/abs/with\nnewline", "x")],
)
def test_create_arguments_refuse_malformed_seed_file_paths(
    tmp_path: Path, seed: tuple[str, str]
) -> None:
    with pytest.raises(InvalidInputError, match="seed file"):
        create_arguments(make_spec(tmp_path, seed_files=(seed,)), INNER, nonce=NONCE)


def test_create_arguments_refuse_an_operation_id_that_reads_as_a_flag(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, operation_id="--deadline-seconds")

    with pytest.raises(InvalidInputError, match="not a flag"):
        create_arguments(spec, INNER, nonce=NONCE)


# --- what the engine reports back ----------------------------------------------------


def _created(fake: FakeDocker, root: Path, **overrides: Any) -> tuple[Any, dict[str, Any]]:
    spec = make_spec(root, **overrides)
    fake.create_worker(spec)
    return spec, fake.containers[spec.container_name].inspect


def test_a_container_created_from_the_arguments_satisfies_the_profile_exactly(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec, _ = _created(fake_docker, tmp_path)

    effective = read_effective(fake_docker.engine(), spec.container_name)

    assert profile_violations(spec.profile, effective) == ()
    assert effective.user == "65532:65532"
    assert effective.cap_drop == ("ALL",)
    assert effective.cap_add == ()
    assert effective.security_options == ("no-new-privileges",)
    assert effective.read_only_rootfs and not effective.privileged
    assert effective.memory_bytes == effective.memory_swap_bytes == 4096 * 1024 * 1024
    assert effective.nano_cpus == 2_000_000_000
    assert effective.pids_limit == 512
    assert effective.network_mode == "enginery-runner-internal"
    assert effective.networks == ("enginery-runner-internal",)
    assert effective.mounts == (
        ("bind", "/out", True),
        ("bind", "/workspace", True),
        ("tmpfs", "/home/worker", True),
        ("tmpfs", "/tmp", True),
    )
    assert effective.devices == ()
    assert effective.env_keys == ("DISABLE_TELEMETRY",)


def test_environment_values_are_never_part_of_the_effective_record(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec, _ = _created(
        fake_docker,
        tmp_path,
        env=(("ANTHROPIC_AUTH_TOKEN", "dummy-relay-credential-value"),),
    )

    effective = read_effective(fake_docker.engine(), spec.container_name)

    assert effective.env_keys == ("ANTHROPIC_AUTH_TOKEN",)
    assert "dummy-relay-credential-value" not in json.dumps(effective.to_state())


def _without(arguments: Sequence[str], flag: str, *, has_value: bool = True) -> list[str]:
    index = arguments.index(flag)
    return [*arguments[:index], *arguments[index + (2 if has_value else 1) :]]


@pytest.mark.parametrize(
    ("flag", "has_value", "violation"),
    [
        ("--user", True, "user is"),
        ("--cap-drop", True, "capabilities are not all dropped"),
        ("--security-opt", True, "no-new-privileges"),
        ("--read-only", False, "root filesystem is writable"),
        ("--memory", True, "memory limit"),
        ("--memory-swap", True, "swap"),
        ("--cpus", True, "cpu limit"),
        ("--pids-limit", True, "pids limit"),
        ("--network", True, "internal runner network"),
    ],
)
def test_a_container_missing_any_hardening_flag_is_reported_as_a_violation(
    fake_docker: FakeDocker, tmp_path: Path, flag: str, has_value: bool, violation: str
) -> None:
    spec = make_spec(tmp_path)
    arguments = _without(create_arguments(spec, INNER, nonce=NONCE), flag, has_value=has_value)
    assert fake_docker.engine().run_cli(arguments).returncode == 0

    effective = read_effective(fake_docker.engine(), spec.container_name)

    assert any(violation in problem for problem in profile_violations(spec.profile, effective))


@pytest.mark.parametrize(
    ("extra", "violation"),
    [
        (("--privileged",), "privileged"),
        (("--cap-add", "NET_ADMIN"), "adds capabilities"),
        (("--device", "/dev/kvm:/dev/kvm"), "host devices"),
        (("-v", "/etc:/host-etc:ro"), "unadmitted mount"),
        (("-e", "GH_TOKEN=ghp_stray"), "environment keys outside the allowlist"),
    ],
)
def test_a_container_with_an_extra_grant_is_reported_as_a_violation(
    fake_docker: FakeDocker, tmp_path: Path, extra: tuple[str, ...], violation: str
) -> None:
    spec = make_spec(tmp_path)
    arguments = create_arguments(spec, INNER, nonce=NONCE)
    assert fake_docker.engine().run_cli(("create", *extra, *arguments[1:])).returncode == 0

    effective = read_effective(fake_docker.engine(), spec.container_name)

    assert any(violation in problem for problem in profile_violations(spec.profile, effective))


def test_effective_state_reads_a_realistic_engine_report() -> None:
    container: dict[str, Any] = {
        "Image": IMAGE_ID,
        "Config": {
            "User": "65532:65532",
            "Env": ["HOME=/home/worker", "PATH=/usr/bin", "NODE_VERSION=22", "HTTP_PROXY=http://p"],
        },
        "HostConfig": {
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "ReadonlyRootfs": True,
            "Memory": 4294967296,
            "MemorySwap": 4294967296,
            "NanoCpus": 2000000000,
            "PidsLimit": None,
            "NetworkMode": "enginery-runner-internal",
            "Tmpfs": {"/tmp": "rw,nosuid,size=512m", "/scratch": "ro,size=1m"},
            "Binds": ["/host/ws:/workspace:rw", "/host/ro:/readonly:ro"],
            "Devices": [{"PathOnHost": "/dev/fuse", "PathInContainer": "/dev/fuse"}],
        },
        "Mounts": [
            {"Type": "bind", "Destination": "/workspace", "RW": True},
            {"Type": "volume", "Destination": "/data", "RW": True},
        ],
        "NetworkSettings": {"Networks": {"enginery-runner-internal": {}, "bridge": {}}},
    }
    image = {"Config": {"Env": ["PATH=/usr/bin", "NODE_VERSION=22"]}}

    effective = effective_from_inspect(container, image)

    assert effective.security_options == ("no-new-privileges",)
    assert effective.pids_limit == 0
    assert effective.networks == ("bridge", "enginery-runner-internal")
    assert effective.mounts == (
        ("bind", "/readonly", False),
        ("bind", "/workspace", True),
        ("tmpfs", "/scratch", False),
        ("tmpfs", "/tmp", True),
        ("volume", "/data", True),
    )
    assert effective.devices == ("/dev/fuse:/dev/fuse",)
    # Entries the image defines are not injected; HOME and HTTP_PROXY are.
    assert effective.env_keys == ("HOME", "HTTP_PROXY")


def test_environment_the_image_itself_defines_is_not_counted_as_injected() -> None:
    container = {"Config": {"Env": ["GPG_KEY=abc", "PYTHON_VERSION=3.12", "HOME=/home/worker"]}}
    image = {"Config": {"Env": ["GPG_KEY=abc", "PYTHON_VERSION=3.12"]}}

    assert effective_from_inspect(container, image).env_keys == ("HOME",)
    assert effective_from_inspect(container, None).env_keys == ("GPG_KEY", "HOME", "PYTHON_VERSION")


def test_an_environment_value_that_differs_from_the_image_counts_as_injected() -> None:
    container = {"Config": {"Env": ["GPG_KEY=attacker"]}}
    image = {"Config": {"Env": ["GPG_KEY=abc"]}}

    assert effective_from_inspect(container, image).env_keys == ("GPG_KEY",)


def test_an_unreadable_engine_report_never_looks_hardened() -> None:
    effective = effective_from_inspect({})
    problems = profile_violations(RunnerProfile(), effective)

    assert any("user is" in problem for problem in problems)
    assert any("root filesystem is writable" in problem for problem in problems)
    assert any("memory limit" in problem for problem in problems)
    assert any("internal runner network" in problem for problem in problems)


def _clean_container(fake: FakeDocker, spec: LaunchSpec) -> dict[str, Any]:
    fake.create_worker(spec)
    return copy.deepcopy(fake.containers[spec.container_name].inspect)


def _weakened(container: dict[str, Any], **host_changes: Any) -> dict[str, Any]:
    weakened = copy.deepcopy(container)
    weakened["HostConfig"].update(host_changes)
    return weakened


def test_a_container_the_launcher_asked_for_has_no_launch_violation(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)

    assert launch_violations(spec, _clean_container(fake_docker, spec)) == ()


@pytest.mark.parametrize(
    ("label", "change"),
    [
        ("host pid", {"PidMode": "host"}),
        ("host ipc", {"IpcMode": "host"}),
        ("host uts", {"UTSMode": "host"}),
        ("userns", {"UsernsMode": "host"}),
        ("host cgroup namespace", {"CgroupnsMode": "host"}),
        ("unset cgroup namespace", {"CgroupnsMode": ""}),
        ("volumes from", {"VolumesFrom": ["other"]}),
        ("links", {"Links": ["other:alias"]}),
        ("gpu", {"DeviceRequests": [{"Driver": "nvidia"}]}),
        ("ports", {"PortBindings": {"80/tcp": [{"HostPort": "80"}]}}),
        ("publish all", {"PublishAllPorts": True}),
        ("sysctls", {"Sysctls": {"net.ipv4.ip_forward": "1"}}),
        ("unmasked paths", {"MaskedPaths": []}),
        ("masked paths missing", {"MaskedPaths": None}),
        ("masked paths partly removed", {"MaskedPaths": ["/proc/acpi"]}),
        ("writable system paths", {"ReadonlyPaths": []}),
        ("read-only paths missing", {"ReadonlyPaths": None}),
        ("another runtime", {"Runtime": "kata"}),
        ("no runtime reported", {"Runtime": ""}),
        ("log driver none", {"LogConfig": {"Type": "none", "Config": {}}}),
        ("unbounded log", {"LogConfig": {"Type": "json-file", "Config": {}}}),
        (
            "log size unbounded",
            {"LogConfig": {"Type": "json-file", "Config": {"max-file": "2"}}},
        ),
        (
            "log file count",
            {"LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "9"}}},
        ),
        ("no per-file limit", {"Ulimits": None}),
        (
            "larger per-file limit",
            {"Ulimits": [{"Name": "fsize", "Soft": 1 << 40, "Hard": 1 << 40}]},
        ),
        ("tmp too large", {"Tmpfs": {"/tmp": "rw,nosuid,nodev,exec,size=4096m"}}),
        ("tmp unsized", {"Tmpfs": {"/tmp": "rw,nosuid,nodev,exec"}}),
        ("tmp suid", {"Tmpfs": {"/tmp": "rw,nodev,exec,size=512m"}}),
        ("tmp with devices", {"Tmpfs": {"/tmp": "rw,nosuid,exec,size=512m"}}),
        ("tmp missing", {"Tmpfs": {}}),
        (
            "home too large",
            {"Tmpfs": {"/home/worker": "rw,nosuid,nodev,exec,size=9999m,uid=65532,gid=65532"}},
        ),
        (
            "home owned by someone else",
            {"Tmpfs": {"/home/worker": "rw,nosuid,nodev,exec,size=512m,uid=0,gid=0"}},
        ),
    ],
)
def test_launch_violations_catch_every_weaker_container(
    fake_docker: FakeDocker, tmp_path: Path, label: str, change: dict[str, Any]
) -> None:
    spec = make_spec(tmp_path)
    container = _clean_container(fake_docker, spec)
    host_changes = dict(change)
    if change.get("Tmpfs"):  # a partial change keeps the other scratch mount as created
        host_changes["Tmpfs"] = {**container["HostConfig"]["Tmpfs"], **change["Tmpfs"]}

    assert launch_violations(spec, _weakened(container, **host_changes)), label


def test_tmpfs_sizes_are_read_back_whatever_unit_the_engine_reports(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    container = _clean_container(fake_docker, spec)
    resources = spec.profile.resources
    reported = {
        "/tmp": f"rw,nosuid,nodev,exec,size={resources.tmp_mib * 1024}k",
        "/home/worker": (
            f"rw,nosuid,nodev,exec,size={resources.home_mib * 1024 * 1024},uid=65532,gid=65532"
        ),
    }

    assert launch_violations(spec, _weakened(container, Tmpfs=reported)) == ()


def test_launch_violations_catch_a_container_from_another_image(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    container = _clean_container(fake_docker, spec)
    container["Image"] = "sha256:" + "9" * 64

    assert launch_violations(spec, container) == (
        "container was not created from the pinned image id",
    )


@pytest.mark.parametrize(
    "option",
    [
        "seccomp=unconfined",
        "apparmor=unconfined",
        "label=disable",
        "systempaths=unconfined",
        "seccomp=/tmp/permissive.json",
    ],
)
def test_a_security_option_beyond_the_profile_is_a_violation(option: str) -> None:
    profile = RunnerProfile()
    effective = dataclasses.replace(
        effective_from_inspect(
            {
                "HostConfig": {"SecurityOpt": ["no-new-privileges", option]},
                "Config": {"User": "65532:65532"},
            }
        ),
    )

    problems = profile_violations(profile, effective)

    assert any(option in problem for problem in problems)


# --- a real engine -------------------------------------------------------------------


def _start_and_collect(
    engine: OciEngine, name: str, out: Path, result_name: str = "result.json"
) -> tuple[int, dict[str, Any]]:
    engine.start(name)
    code = engine.wait(name)
    result = json.loads((out / result_name).read_text(encoding="utf-8"))
    return code, result


def test_real_engine_container_from_create_arguments_satisfies_the_profile(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = make_spec(
        real_root, operation_id=real_operation(), image=standin_image, profile=real_profile
    )
    real_engine.ensure_internal_network(real_profile.network)

    created = real_engine.run_cli(
        create_arguments(spec, ("python3", "-c", "print(1)"), nonce=NONCE)
    )
    assert created.returncode == 0, created.stderr
    assert created.stderr == ""

    container = real_engine.container_inspect(spec.container_name)
    effective = read_effective(real_engine, spec.container_name)
    assert profile_violations(real_profile, effective) == ()
    assert launch_violations(spec, container) == ()
    assert container["State"]["Status"] == "created"
    assert effective.networks == (real_profile.network,)
    assert effective.env_keys == ("DISABLE_TELEMETRY",)


def test_real_engine_reports_a_container_with_an_added_capability_as_weaker(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = make_spec(
        real_root, operation_id=real_operation(), image=standin_image, profile=real_profile
    )
    real_engine.ensure_internal_network(real_profile.network)
    arguments = create_arguments(spec, ("python3", "-c", "print(1)"), nonce=NONCE)

    created = real_engine.run_cli(("create", "--cap-add", "NET_ADMIN", *arguments[1:]))
    assert created.returncode == 0, created.stderr

    effective = read_effective(real_engine, spec.container_name)
    assert [capability.removeprefix("CAP_") for capability in effective.cap_add] == ["NET_ADMIN"]


def test_real_worker_runs_as_the_profile_uid_writes_the_workspace_and_the_host_can_delete_it(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    script = (
        "import os\n"
        "open('/workspace/proof.txt', 'w').write('uid=%d gid=%d' % (os.getuid(), os.getgid()))\n"
        "os.mkdir('/workspace/nested')\n"
        "open('/workspace/nested/inner.txt', 'w').write('nested')\n"
        "open('/workspace/existing.txt', 'a').write(' + worker')\n"
        "print(open('/workspace/seed.txt', encoding='utf-8').read())\n"
        "try:\n"
        "    open('/etc/probe', 'w')\n"
        "except OSError as error:\n"
        "    print('rootfs', error.errno)\n"
    )
    spec = make_spec(
        real_root,
        operation_id=real_operation(),
        image=standin_image,
        profile=real_profile,
        seed_files=(("/workspace/seed.txt", "seed with sémantique ✓"),),
    )
    real_engine.ensure_internal_network(real_profile.network)
    # The workspace belongs to the operator; the launcher makes it writable for the worker.
    (real_root / "workspace" / "existing.txt").write_text("from the host", encoding="utf-8")
    (real_root / "workspace" / "existing.txt").chmod(0o600)
    make_tree_writable(spec.workspace_path)
    make_tree_writable(spec.out_path)
    created = real_engine.run_cli(create_arguments(spec, ("python3", "-c", script), nonce=NONCE))
    assert created.returncode == 0, created.stderr

    code, result = _start_and_collect(real_engine, spec.container_name, real_root / "out")

    assert code == 0
    assert result["terminal_status"] == "succeeded"
    assert "seed with sémantique ✓" in result["output"]
    assert "rootfs 30" in result["output"]  # EROFS: the root filesystem is read-only
    workspace = real_root / "workspace"
    assert (workspace / "proof.txt").read_text() == "uid=65532 gid=65532"
    assert (workspace / "existing.txt").read_text() == "from the host + worker"
    # Everything the worker made (it runs with umask 0) must be deletable by the host, whatever
    # owner the engine's file sharing gives new entries: Docker Desktop re-owns them to the
    # operator, a rootless engine keeps a sub-uid owner.
    shutil.rmtree(workspace / "nested")
    (workspace / "proof.txt").unlink()
    (workspace / "seed.txt").unlink()
    (real_root / "out" / "result.json").unlink()
    assert not os.path.lexists(workspace / "nested")


def test_real_worker_cannot_reach_the_internet_on_the_internal_network(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    script = (
        "import socket\n"
        "for host in ('1.1.1.1', '8.8.8.8'):\n"
        "    try:\n"
        "        socket.create_connection((host, 80), timeout=3)\n"
        "    except OSError as error:\n"
        "        print('unreachable', host, error.errno)\n"
        "    else:\n"
        "        print('CONNECTED', host)\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 80)\n"
        "except OSError:\n"
        "    print('no-dns')\n"
        "else:\n"
        "    print('DNS-RESOLVED')\n"
    )
    spec = make_spec(
        real_root, operation_id=real_operation(), image=standin_image, profile=real_profile
    )
    real_engine.ensure_internal_network(real_profile.network)
    make_tree_writable(spec.workspace_path)
    make_tree_writable(spec.out_path)
    created = real_engine.run_cli(create_arguments(spec, ("python3", "-c", script), nonce=NONCE))
    assert created.returncode == 0, created.stderr

    code, result = _start_and_collect(real_engine, spec.container_name, real_root / "out")

    assert code == 0
    output = result["output"]
    assert "CONNECTED" not in output
    assert "DNS-RESOLVED" not in output
    assert "unreachable 1.1.1.1" in output
    assert "unreachable 8.8.8.8" in output
    assert "no-dns" in output
