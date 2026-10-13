from __future__ import annotations

import itertools
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci.engine import EngineCommandError, OciEngine
from enginery.adapters.oci.launch import probe_arguments
from enginery.adapters.oci.probe import OciProbeRunner
from enginery.domain.errors import (
    InvalidInputError,
    MissingPrerequisiteError,
    OperationTimeoutError,
)
from enginery.domain.runner import ResourceLimits, RunnerProfile

from .support import IMAGE_ID, FakeDocker


def _flag_values(arguments: Sequence[str], flag: str) -> list[str]:
    return [value for key, value in itertools.pairwise(arguments) if key == flag]


# --- the command line ----------------------------------------------------------------


def test_probe_arguments_are_hardened_networkless_and_mountless() -> None:
    profile = RunnerProfile()

    arguments = probe_arguments(profile, IMAGE_ID, ("omp", "--help"), name="enginery-probe-x")

    assert arguments == (
        "run",
        "--rm",
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
        "none",
        "--ipc",
        "private",
        "--init",
        "--label",
        "dev.enginery.probe=1",
        "--name",
        "enginery-probe-x",
        "-w",
        "/home/worker",
        "-e",
        "HOME=/home/worker",
        IMAGE_ID,
        "omp",
        "--help",
    )
    assert "-v" not in arguments
    assert "--mount" not in arguments
    assert "--volume" not in arguments


def test_probe_hardening_follows_the_profile() -> None:
    profile = RunnerProfile(
        uid=70000,
        gid=70000,
        resources=ResourceLimits(memory_mib=512, cpus=0.5, pids=32, tmp_mib=16, home_mib=8),
    )

    arguments = probe_arguments(profile, IMAGE_ID, ("claude", "--version"), name="p")

    assert _flag_values(arguments, "--user") == ["70000:70000"]
    assert _flag_values(arguments, "--memory") == ["512m"]
    assert _flag_values(arguments, "--cpus") == ["0.5"]
    assert _flag_values(arguments, "--pids-limit") == ["32"]
    assert _flag_values(arguments, "--network") == ["none"]
    assert _flag_values(arguments, "--tmpfs") == [
        "/tmp:rw,nosuid,nodev,exec,size=16m",
        "/home/worker:rw,nosuid,nodev,exec,size=8m,uid=70000,gid=70000",
    ]


@pytest.mark.parametrize("command", [(), ("",), ("omp", "")])
def test_probe_arguments_refuse_a_blank_command(command: tuple[str, ...]) -> None:
    with pytest.raises(InvalidInputError):
        probe_arguments(RunnerProfile(), IMAGE_ID, command, name="p")


# --- running -------------------------------------------------------------------------


def test_probe_returns_what_the_contained_command_printed(fake_docker: FakeDocker) -> None:
    fake_docker.log_text = "omp v18.10.3\nusage: omp ...\n"
    probe = OciProbeRunner(fake_docker.engine(), IMAGE_ID, RunnerProfile())

    completed = probe(("omp", "--help"))

    assert completed.returncode == 0
    assert completed.stdout == "omp v18.10.3\nusage: omp ...\n"
    assert completed.args == ("omp", "--help")
    (command,) = fake_docker.calls_of("run")
    assert command[command.index(IMAGE_ID) :] == (IMAGE_ID, "omp", "--help")


def test_probe_reports_a_failing_command_as_that_commands_own_status(
    fake_docker: FakeDocker,
) -> None:
    fake_docker.exit_code = 3
    probe = OciProbeRunner(fake_docker.engine(), IMAGE_ID, RunnerProfile())

    assert probe(("claude", "--version")).returncode == 3


def test_probe_raises_when_the_container_could_not_be_started(fake_docker: FakeDocker) -> None:
    fake_docker.failures["run"] = (125, "docker: Error response from daemon: no such image")
    probe = OciProbeRunner(fake_docker.engine(), IMAGE_ID, RunnerProfile())

    with pytest.raises(EngineCommandError, match="could not be started") as raised:
        probe(("omp", "--help"))

    assert "no such image" in str(raised.value.details["stderr"])


def test_probe_raises_when_the_engine_cli_cannot_run(tmp_path: Path) -> None:
    engine = OciEngine(executable=str(tmp_path / "missing-docker"))

    with pytest.raises(MissingPrerequisiteError):
        OciProbeRunner(engine, IMAGE_ID, RunnerProfile())(("omp", "--help"))


def test_probe_removes_its_container_when_the_client_times_out(fake_docker: FakeDocker) -> None:
    started: list[tuple[str, ...]] = []

    def hanging(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "run" in argv:
            started.append(tuple(argv))
            raise subprocess.TimeoutExpired(list(argv), 1)
        return fake_docker(argv, **kwargs)

    probe = OciProbeRunner(OciEngine(runner=hanging), IMAGE_ID, RunnerProfile(), timeout_seconds=1)

    with pytest.raises(OperationTimeoutError):
        probe(("omp", "--help"))

    (run_command,) = started
    (removal,) = fake_docker.calls_of("rm")
    assert removal == ("rm", "--volumes", "--force", _flag_values(run_command, "--name")[0])


def test_each_probe_uses_its_own_container_name(fake_docker: FakeDocker) -> None:
    probe = OciProbeRunner(fake_docker.engine(), IMAGE_ID, RunnerProfile())

    probe(("omp", "--help"))
    probe(("omp", "--help"))

    names = [_flag_values(command, "--name")[0] for command in fake_docker.calls_of("run")]
    assert len(set(names)) == 2
    assert all(name.startswith("enginery-probe-") for name in names)


def test_probe_refuses_an_image_that_is_not_an_exact_id() -> None:
    with pytest.raises(InvalidInputError, match="exact image id"):
        OciProbeRunner(OciEngine(), "python:3.12-slim", RunnerProfile())
    with pytest.raises(InvalidInputError, match="timeout"):
        OciProbeRunner(OciEngine(), IMAGE_ID, RunnerProfile(), timeout_seconds=0)


# --- a real engine -------------------------------------------------------------------


def test_real_probe_runs_hardened_without_network_or_mounts_and_leaves_nothing_behind(
    real_engine: OciEngine, real_profile: RunnerProfile, standin_image: str
) -> None:
    probe = OciProbeRunner(real_engine, standin_image, real_profile)
    script = (
        "import os, socket\n"
        "lines = open('/proc/self/status').read().splitlines()\n"
        "status = dict(line.split(':', 1) for line in lines if ':' in line)\n"
        "print('uid', status['Uid'].split()[0])\n"
        "print('cap', status['CapEff'].strip())\n"
        "print('nnp', status['NoNewPrivs'].strip())\n"
        "print('workspace', os.path.exists('/workspace'), os.path.exists('/out'))\n"
        "print('mounts', [line.split()[4] for line in open('/proc/self/mountinfo') "
        "if line.split()[4] in ('/workspace', '/out', '/Users', '/host_mnt')])\n"
        "try:\n"
        "    open('/etc/probe', 'w')\n"
        "except OSError as error:\n"
        "    print('rootfs', error.errno)\n"
        "open('/home/worker/ok', 'w').write('x')\n"
        "print('home-writable')\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "except OSError as error:\n"
        "    print('net', error.errno)\n"
        "else:\n"
        "    print('CONNECTED')\n"
    )
    before = real_engine.run_cli(("ps", "--all", "--filter", "label=dev.enginery.probe=1", "-q"))

    completed = probe(("python3", "-c", script))

    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert "uid 65532" in lines
    assert "cap 0000000000000000" in lines
    assert "nnp 1" in lines
    assert "workspace False False" in lines
    assert "mounts []" in lines
    assert "rootfs 30" in lines
    assert "home-writable" in lines
    assert "CONNECTED" not in lines
    assert any(line.startswith("net ") for line in lines)
    after = real_engine.run_cli(("ps", "--all", "--filter", "label=dev.enginery.probe=1", "-q"))
    assert after.stdout == before.stdout


def test_real_probe_reports_the_contained_commands_failure_and_a_missing_command(
    real_engine: OciEngine, real_profile: RunnerProfile, standin_image: str
) -> None:
    probe = OciProbeRunner(real_engine, standin_image, real_profile)

    failing = probe(("python3", "-c", "import sys; sys.exit(7)"))
    missing = probe(("no-such-harness", "--version"))

    assert failing.returncode == 7
    assert missing.returncode != 0
    assert missing.stdout == ""


def test_real_probe_of_an_absent_image_raises_instead_of_reporting_success(
    real_engine: OciEngine, real_profile: RunnerProfile
) -> None:
    probe = OciProbeRunner(real_engine, "sha256:" + "0" * 64, real_profile)

    with pytest.raises(EngineCommandError, match="could not be started"):
        probe(("omp", "--help"))
