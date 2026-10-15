from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci.engine import (
    ENGINE_ENVIRONMENT_KEYS,
    KIND_DOCKER,
    KIND_DOCKER_DESKTOP,
    KIND_ROOTLESS_DOCKER,
    EngineCommandError,
    ObjectNotFoundError,
    OciEngine,
    engine_environment,
)
from enginery.adapters.oci.launch import container_name_for
from enginery.domain.errors import MissingPrerequisiteError, OperationTimeoutError

from .support import DOCKER_DESKTOP_INFO, IMAGE_ID, ROOTLESS_INFO, FakeDocker, make_spec


def _completed(
    argv: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(tuple(argv), returncode, stdout, stderr)


# --- command execution ---------------------------------------------------------------


def test_run_cli_prepends_the_executable_and_the_context(fake_docker: FakeDocker) -> None:
    engine = fake_docker.engine(context="desktop-linux")

    engine.run_cli(("info", "--format", "{{json .}}"))

    assert fake_docker.context == "desktop-linux"
    assert fake_docker.calls == [("info", "--format", "{{json .}}")]


def test_run_cli_without_a_context_adds_no_context_flag() -> None:
    seen: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(tuple(argv))
        return _completed(argv)

    OciEngine(runner=runner, executable="podman-docker").run_cli(("ps",))

    assert seen == [("podman-docker", "ps")]


def test_run_cli_returns_a_failing_exit_instead_of_raising() -> None:
    engine = OciEngine(runner=lambda argv, **_: _completed(argv, 3, "", "boom"))

    completed = engine.run_cli(("ps",))

    assert (completed.returncode, completed.stderr) == (3, "boom")


def test_run_cli_reports_a_missing_engine_cli_as_a_missing_prerequisite(tmp_path: Path) -> None:
    engine = OciEngine(executable=str(tmp_path / "no-such-docker"))

    with pytest.raises(MissingPrerequisiteError, match="cannot be run"):
        engine.run_cli(("info",))


def test_run_cli_reports_a_timeout_as_an_operation_timeout() -> None:
    def runner(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 1)

    with pytest.raises(OperationTimeoutError):
        OciEngine(runner=runner).run_cli(("build", "x"), timeout=1)


def test_engine_environment_keeps_only_the_engine_connection_variables() -> None:
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "DOCKER_HOST": "unix:///run/docker.sock",
        "GH_TOKEN": "ghp_not_for_containers",
        "ANTHROPIC_API_KEY": "sk-ant-nope",
        "OMP_AUTH_BROKER_TOKEN": "nope",
        "AWS_SECRET_ACCESS_KEY": "nope",
    }

    assert engine_environment(environment) == {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "DOCKER_HOST": "unix:///run/docker.sock",
    }
    assert set(ENGINE_ENVIRONMENT_KEYS) == {
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "XDG_RUNTIME_DIR",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
    }


def test_default_runner_gives_the_real_subprocess_only_the_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'env': dict(os.environ), 'argv': sys.argv[1:], "
        "'stdin': sys.stdin.read()}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    for name, value in {
        "GH_TOKEN": "ghp_secret",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OMP_AUTH_BROKER_TOKEN": "broker-secret",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "DOCKER_HOST": "tcp://engine.example:2376",
        "DOCKER_CONTEXT": "remote",
        "DOCKER_TLS_VERIFY": "1",
    }.items():
        monkeypatch.setenv(name, value)

    engine = OciEngine(executable=str(script))
    plain = json.loads(engine.run_cli(("version",)).stdout)
    fed = json.loads(engine.run_cli(("build", "-"), input="FROM scratch\n").stdout)

    for report in (plain, fed):
        # The fake is a Python script: the interpreter itself adds locale variables on some
        # platforms. Nothing from the (secret-bearing) host environment may appear.
        extra = set(report["env"]) - set(ENGINE_ENVIRONMENT_KEYS)
        assert extra <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
        for secret in ("GH_TOKEN", "ANTHROPIC_API_KEY", "OMP_AUTH_BROKER_TOKEN", "SSH_AUTH_SOCK"):
            assert secret not in report["env"]
        assert report["env"]["DOCKER_HOST"] == "tcp://engine.example:2376"
        assert report["env"]["DOCKER_CONTEXT"] == "remote"
        assert report["env"]["DOCKER_TLS_VERIFY"] == "1"
    assert plain["argv"] == ["version"]
    assert plain["stdin"] == ""
    assert fed["stdin"] == "FROM scratch\n"


def test_default_runner_decodes_binary_output_instead_of_raising(tmp_path: Path) -> None:
    script = tmp_path / "docker"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'ok \\xff\\xfe tail')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    completed = OciEngine(executable=str(script)).run_cli(("logs", "x"))

    assert completed.stdout.startswith("ok ")
    assert completed.stdout.endswith(" tail")


# --- identity and qualification ------------------------------------------------------


@pytest.mark.parametrize(
    ("info", "kind", "rootless"),
    [
        (DOCKER_DESKTOP_INFO, KIND_DOCKER_DESKTOP, False),
        (ROOTLESS_INFO, KIND_ROOTLESS_DOCKER, True),
        (
            {**DOCKER_DESKTOP_INFO, "OperatingSystem": "Ubuntu 24.04", "ServerVersion": "28.0.1"},
            KIND_DOCKER,
            False,
        ),
    ],
)
def test_identity_classifies_the_engine_from_what_it_reports(
    info: dict[str, Any], kind: str, rootless: bool
) -> None:
    identity = FakeDocker(info=info).engine(context="ctx").identity()

    assert identity.kind == kind
    assert identity.rootless is rootless
    assert identity.server_version == info["ServerVersion"]
    assert identity.operating_system == info["OperatingSystem"]
    assert identity.kernel_version == info["KernelVersion"]
    assert identity.cgroup_version == "2"
    assert identity.security_options == tuple(info["SecurityOptions"])
    assert identity.context == "ctx"


def test_identity_of_an_unreachable_engine_is_a_missing_prerequisite() -> None:
    fake = FakeDocker()
    fake.unreachable = True

    with pytest.raises(MissingPrerequisiteError, match="unreachable"):
        fake.engine().identity()


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", json.dumps({"ServerErrors": ["Cannot connect"]}), json.dumps({"Name": "x"})],
)
def test_identity_refuses_unusable_info_output(stdout: str) -> None:
    engine = OciEngine(runner=lambda argv, **_: _completed(argv, 0, stdout))

    with pytest.raises(MissingPrerequisiteError):
        engine.identity()


def test_memory_total_is_what_the_engine_reports() -> None:
    assert FakeDocker().engine().memory_total_bytes() == 20_449_584 * 1024


@pytest.mark.parametrize("value", [None, 0, -5, "8589934592", True, 1.5])
def test_an_engine_without_a_usable_memory_total_is_a_missing_prerequisite(value: object) -> None:
    fake = FakeDocker()
    fake.info["MemTotal"] = value

    with pytest.raises(MissingPrerequisiteError, match="no usable total memory"):
        fake.engine().memory_total_bytes()


def test_qualify_accepts_an_engine_that_can_enforce_the_profile(fake_docker: FakeDocker) -> None:
    assert fake_docker.engine().qualify().kind == KIND_DOCKER_DESKTOP
    assert FakeDocker(info=ROOTLESS_INFO).engine().qualify().rootless is True


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        ({"CgroupVersion": "1"}, "cgroup v2"),
        ({"CgroupVersion": ""}, "cgroup v2"),
        ({"SecurityOptions": ["name=cgroupns"]}, "seccomp"),
        ({"SecurityOptions": ["name=seccomp,profile=unconfined"]}, "seccomp"),
        ({"MemoryLimit": False}, "memory limit"),
        ({"SwapLimit": False}, "swap limit"),
        ({"CpuCfsQuota": False}, "cpu cfs quota"),
        ({"CpuCfsPeriod": False}, "cpu cfs period"),
        ({"PidsLimit": False}, "pids limit"),
        ({"Warnings": ["WARNING: No memory limit support"]}, "No memory limit support"),
        ({"Warnings": ["WARNING: No swap limit support"]}, "No swap limit support"),
        ({"Warnings": ["WARNING: No cpu cfs quota support"]}, "No cpu cfs quota support"),
    ],
)
def test_qualify_refuses_an_engine_missing_a_requirement_and_names_it(
    change: dict[str, Any], missing: str
) -> None:
    fake = FakeDocker(info={**DOCKER_DESKTOP_INFO, **change})

    with pytest.raises(MissingPrerequisiteError, match="cannot enforce") as raised:
        fake.engine().qualify()

    assert missing in str(raised.value)
    assert missing in str(raised.value.details["missing"])


def test_qualify_names_every_missing_requirement_at_once() -> None:
    fake = FakeDocker(
        info={
            **DOCKER_DESKTOP_INFO,
            "CgroupVersion": "1",
            "MemoryLimit": False,
            "SecurityOptions": [],
        }
    )

    with pytest.raises(MissingPrerequisiteError) as raised:
        fake.engine().qualify()

    message = str(raised.value)
    assert "cgroup v2" in message
    assert "seccomp" in message
    assert "memory limit" in message


def test_qualify_does_not_ignore_unrelated_engine_warnings_but_is_not_blocked_by_them() -> None:
    fake = FakeDocker(
        info={**DOCKER_DESKTOP_INFO, "Warnings": ["WARNING: bridge-nf-call-iptables is disabled"]}
    )

    assert fake.engine().qualify().kind == KIND_DOCKER_DESKTOP


# --- networks ------------------------------------------------------------------------


def test_ensure_internal_network_creates_an_internal_network_when_absent(
    fake_docker: FakeDocker,
) -> None:
    fake_docker.engine().ensure_internal_network("runner-net")

    assert fake_docker.networks == {"runner-net": True}
    created = fake_docker.calls_of("network create")
    assert len(created) == 1
    assert "--internal" in created[0]
    assert created[0][-1] == "runner-net"


def test_ensure_internal_network_is_idempotent(fake_docker: FakeDocker) -> None:
    engine = fake_docker.engine()

    engine.ensure_internal_network("runner-net")
    engine.ensure_internal_network("runner-net")

    assert len(fake_docker.calls_of("network create")) == 1
    assert fake_docker.networks == {"runner-net": True}


def test_ensure_internal_network_refuses_an_existing_network_that_is_not_internal(
    fake_docker: FakeDocker,
) -> None:
    fake_docker.networks["runner-net"] = False

    with pytest.raises(MissingPrerequisiteError, match="not internal"):
        fake_docker.engine().ensure_internal_network("runner-net")

    assert fake_docker.calls_of("network create") == []


def test_ensure_internal_network_tolerates_a_concurrent_creator(fake_docker: FakeDocker) -> None:
    real_runner = fake_docker.__call__

    def racing(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "create" in argv:
            # Another process creates the network between our inspect and our create.
            fake_docker.networks["runner-net"] = True
        return real_runner(argv, **kwargs)

    OciEngine(runner=racing).ensure_internal_network("runner-net")

    assert fake_docker.networks == {"runner-net": True}


def test_ensure_internal_network_refuses_when_the_racing_network_is_not_internal(
    fake_docker: FakeDocker,
) -> None:
    real_runner = fake_docker.__call__

    def racing(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "create" in argv:
            fake_docker.networks["runner-net"] = False
        return real_runner(argv, **kwargs)

    with pytest.raises(MissingPrerequisiteError, match="not internal"):
        OciEngine(runner=racing).ensure_internal_network("runner-net")


def test_ensure_internal_network_surfaces_a_real_create_failure(fake_docker: FakeDocker) -> None:
    fake_docker.failures["network create"] = (1, "Error response from daemon: pool overlaps")

    with pytest.raises(EngineCommandError) as raised:
        fake_docker.engine().ensure_internal_network("runner-net")

    assert "pool overlaps" in str(raised.value.details["stderr"])


# --- images and containers -----------------------------------------------------------


def test_container_inspect_returns_the_engine_document_and_reports_absence(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    engine = fake_docker.engine()
    spec = make_spec(tmp_path)

    with pytest.raises(ObjectNotFoundError):
        engine.container_inspect(spec.container_name)
    assert engine.container_exists(spec.container_name) is False

    fake_docker.create_worker(spec)

    assert engine.container_inspect(spec.container_name)["Name"] == f"/{spec.container_name}"
    assert engine.container_exists(spec.container_name) is True


def test_image_inspect_returns_the_image_and_reports_absence(fake_docker: FakeDocker) -> None:
    engine = fake_docker.engine()

    image_id = next(iter(fake_docker.images))
    assert engine.image_inspect(image_id)["Id"] == image_id
    with pytest.raises(ObjectNotFoundError):
        engine.image_inspect("sha256:" + "f" * 64)


def test_container_exists_does_not_hide_an_engine_failure(fake_docker: FakeDocker) -> None:
    fake_docker.failures["container inspect"] = (1, "Cannot connect to the Docker daemon")

    with pytest.raises(EngineCommandError):
        fake_docker.engine().container_exists("enginery-x")


def test_kill_and_remove_tolerate_a_container_that_is_already_gone(
    fake_docker: FakeDocker,
) -> None:
    engine = fake_docker.engine()

    engine.kill("enginery-gone")
    engine.remove("enginery-gone")

    assert fake_docker.calls_of("kill") == [("kill", "--signal", "KILL", "enginery-gone")]
    assert fake_docker.calls_of("rm") == [("rm", "--volumes", "--force", "enginery-gone")]


def test_kill_and_remove_raise_on_other_engine_failures(fake_docker: FakeDocker) -> None:
    fake_docker.failures["kill"] = (1, "permission denied")
    fake_docker.failures["rm"] = (1, "device or resource busy")
    engine = fake_docker.engine()

    with pytest.raises(EngineCommandError, match="kill"):
        engine.kill("enginery-x")
    with pytest.raises(EngineCommandError, match="rm"):
        engine.remove("enginery-x")


def test_list_runner_containers_returns_only_labelled_workers_sorted(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    engine = fake_docker.engine()
    for index, operation in enumerate(("op-b", "op-a")):
        root = tmp_path / str(index)
        root.mkdir()
        fake_docker.create_worker(make_spec(root, operation_id=operation))
    engine.run_cli(("create", "--name", "someone-elses-container", IMAGE_ID, "true"))

    names = engine.list_runner_containers()

    workers = (container_name_for("op-a"), container_name_for("op-b"))
    # Each attempt has its worker and its store container; both carry the runner label.
    assert names == tuple(sorted((*workers, *(f"{worker}-store" for worker in workers))))
    assert "label=dev.enginery.runner=1" in fake_docker.calls_of("ps")[0]


def test_logs_tail_keeps_only_the_last_bytes_of_long_output(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.create_worker(spec)
    fake_docker.log_text = "début " + "x" * 200_000 + " fin\n"

    tail = fake_docker.engine().logs_tail(spec.container_name, max_bytes=1000)

    assert len(tail.encode()) <= 1000
    assert tail.endswith(" fin\n")
    assert "début" not in tail


def test_wait_returns_the_container_exit_code_and_rejects_garbage() -> None:
    engine = OciEngine(runner=lambda argv, **_: _completed(argv, 0, "137\n"))
    assert engine.wait("enginery-x") == 137

    garbage = OciEngine(runner=lambda argv, **_: _completed(argv, 0, "NaN\n"))
    with pytest.raises(EngineCommandError, match="exit code"):
        garbage.wait("enginery-x")


# --- a real engine -------------------------------------------------------------------


def test_real_engine_identity_matches_what_docker_info_reports(real_engine: OciEngine) -> None:
    raw = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True, check=True
    )
    info = json.loads(raw.stdout)

    identity = real_engine.qualify()

    assert identity.server_version == info["ServerVersion"]
    assert identity.cgroup_version == info["CgroupVersion"] == "2"
    assert identity.rootless is any("rootless" in option for option in info["SecurityOptions"])
    assert identity.kind in {KIND_DOCKER_DESKTOP, KIND_ROOTLESS_DOCKER, KIND_DOCKER}
    assert any(option.startswith("name=seccomp") for option in identity.security_options)
    assert real_engine.memory_total_bytes() == info["MemTotal"] > 0


def test_real_engine_internal_network_is_created_once_and_is_internal(
    real_engine: OciEngine, real_profile: Any
) -> None:
    name = real_profile.network

    real_engine.ensure_internal_network(name)
    real_engine.ensure_internal_network(name)

    inspected = json.loads(real_engine.run_cli(("network", "inspect", name)).stdout)
    assert [network["Internal"] for network in inspected] == [True]


def test_real_engine_refuses_a_same_named_network_that_is_not_internal(
    real_engine: OciEngine,
) -> None:
    name = f"enginery-test-open-{uuid.uuid4().hex[:8]}"
    created = real_engine.run_cli(("network", "create", name))
    assert created.returncode == 0, created.stderr
    try:
        with pytest.raises(MissingPrerequisiteError, match="not internal"):
            real_engine.ensure_internal_network(name)
    finally:
        real_engine.run_cli(("network", "rm", name))


def test_real_engine_kill_and_remove_of_an_absent_container_are_not_errors(
    real_engine: OciEngine,
) -> None:
    real_engine.kill("enginery-test-absent-container")
    real_engine.remove("enginery-test-absent-container")
    assert real_engine.container_exists("enginery-test-absent-container") is False


@pytest.mark.parametrize("omitted", ["KernelVersion", "OperatingSystem"])
def test_an_engine_that_omits_an_identity_field_is_refused_before_any_receipt_exists(
    omitted: str,
) -> None:
    info = {key: value for key, value in DOCKER_DESKTOP_INFO.items() if key != omitted}

    with pytest.raises(MissingPrerequisiteError, match="usable identity"):
        FakeDocker(info=info).engine().qualify()
    with pytest.raises(MissingPrerequisiteError, match="usable identity"):
        FakeDocker(info=info).engine().identity()


# --- volumes and tar streams ---------------------------------------------------------


class _Copier:
    """A ``CopyRunner`` that records what it was asked and replays a canned archive."""

    def __init__(self, *, returncode: int = 0, stderr: str = "", archive: bytes = b"") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.written = b""
        self.read = b""
        self.returncode = returncode
        self.stderr = stderr
        self.archive = archive

    def copy_in(
        self, argv: Sequence[str], writer: Any, *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        buffer = io.BytesIO()
        writer(buffer)
        self.written = buffer.getvalue()
        self.calls.append(tuple(argv))
        return _completed(argv, self.returncode, "", self.stderr)

    def copy_out(
        self, argv: Sequence[str], reader: Any, *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        reader(io.BytesIO(self.archive))
        self.calls.append(tuple(argv))
        return _completed(argv, self.returncode, "", self.stderr)


def test_copy_in_streams_an_owner_preserving_archive_to_the_container_path() -> None:
    copier = _Copier()
    engine = OciEngine(context="ctx", runner=lambda argv, **_: _completed(argv), copier=copier)

    engine.copy_in("worker", "/workspace", lambda stream: stream.write(b"tar-bytes"))

    assert copier.calls == [("docker", "--context", "ctx", "cp", "-a", "-", "worker:/workspace")]
    assert copier.written == b"tar-bytes"


def test_copy_out_streams_the_archive_of_a_container_path_to_the_reader() -> None:
    copier = _Copier(archive=b"payload")
    engine = OciEngine(runner=lambda argv, **_: _completed(argv), copier=copier)
    received: list[bytes] = []

    engine.copy_out("worker", "/out/result.json", lambda stream: received.append(stream.read()))

    assert copier.calls == [("docker", "cp", "worker:/out/result.json", "-")]
    assert received == [b"payload"]


def test_a_copy_of_a_path_the_container_does_not_have_is_an_object_not_found_error() -> None:
    copier = _Copier(
        returncode=1,
        stderr="Error response from daemon: Could not find the file /out/x in container c",
    )
    engine = OciEngine(runner=lambda argv, **_: _completed(argv), copier=copier)

    with pytest.raises(ObjectNotFoundError):
        engine.copy_out("worker", "/out/x", lambda stream: None)


def test_a_failed_copy_in_is_an_engine_error_that_names_the_engines_reason() -> None:
    copier = _Copier(returncode=1, stderr="no space left on device")
    engine = OciEngine(runner=lambda argv, **_: _completed(argv), copier=copier)

    with pytest.raises(EngineCommandError) as raised:
        engine.copy_in("worker", "/workspace", lambda stream: None)

    assert "no space left" in str(raised.value.details["stderr"])


def test_a_runner_that_also_copies_is_used_for_copies_without_a_separate_copier() -> None:
    class Both(_Copier):
        def __call__(self, argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
            return _completed(argv)

    both = Both()

    OciEngine(runner=both).copy_in("worker", "/workspace", lambda stream: None)

    assert both.calls == [("docker", "cp", "-a", "-", "worker:/workspace")]


def test_volume_inspect_returns_the_volume_document() -> None:
    document = {"Name": "v", "Driver": "local", "Options": {"type": "tmpfs"}}
    engine = OciEngine(runner=lambda argv, **_: _completed(argv, 0, json.dumps([document])))

    assert dict(engine.volume_inspect("v")) == document


def test_removing_an_absent_volume_is_not_an_error_but_a_volume_in_use_is() -> None:
    absent = OciEngine(
        runner=lambda argv, **_: _completed(
            argv, 1, "", "Error response from daemon: get v: no such volume"
        )
    )
    absent.volume_remove("v")

    in_use = OciEngine(
        runner=lambda argv, **_: _completed(
            argv, 1, "", "Error response from daemon: remove v: volume is in use"
        )
    )
    with pytest.raises(EngineCommandError):
        in_use.volume_remove("v")


def test_dangling_runner_volumes_are_listed_by_label_and_dangling_filter() -> None:
    seen: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(tuple(argv))
        return _completed(argv, 0, "b\na\n")

    assert OciEngine(runner=runner).list_dangling_runner_volumes() == ("a", "b")
    assert seen == [
        (
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            "label=dev.enginery.runner=1",
            "--filter",
            "dangling=true",
        )
    ]


def test_real_engine_copies_a_tree_through_a_running_container_with_a_capped_tmpfs_volume(
    real_engine: OciEngine, standin_image: str
) -> None:
    suffix = uuid.uuid4().hex[:8]
    container = f"enginery-test-copy-{suffix}"
    volume = f"enginery-test-copy-vol-{suffix}"
    mount = (
        f"type=volume,src={volume},dst=/workspace,volume-nocopy,"
        '"volume-opt=type=tmpfs","volume-opt=device=tmpfs",'
        '"volume-opt=o=size=8m,nr_inodes=64,uid=65532,gid=65532"'
    )
    started = real_engine.run_cli(
        (
            *("run", "-d", "--name", container, "--user", "65532:65532", "--network", "none"),
            *("--mount", mount, standin_image, "sleep", "300"),
        )
    )
    assert started.returncode == 0, started.stderr
    try:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as writer:
            info = tarfile.TarInfo("seed.txt")
            info.size = 5
            info.uid = info.gid = 65532
            writer.addfile(info, io.BytesIO(b"hello"))
        real_engine.copy_in(
            container, "/workspace", lambda stream: stream.write(archive.getvalue())
        )

        names: list[str] = []

        def read(stream: Any) -> None:
            with tarfile.open(fileobj=stream, mode="r|") as reader:
                names.extend(member.name for member in reader)

        real_engine.copy_out(container, "/workspace/.", read)
        assert "./seed.txt" in names or "seed.txt" in names

        with pytest.raises(ObjectNotFoundError):
            real_engine.copy_out(container, "/workspace/absent", lambda stream: stream.read())
        volume_document = real_engine.volume_inspect(volume)
        assert volume_document["Options"] == {
            "device": "tmpfs",
            "o": "size=8m,nr_inodes=64,uid=65532,gid=65532",
            "type": "tmpfs",
        }
    finally:
        real_engine.remove(container)
        real_engine.volume_remove(volume)
