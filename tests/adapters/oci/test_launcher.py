from __future__ import annotations

import dataclasses
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import (
    BUN_LABEL,
    CLAUDE_CODE_LABEL,
    OMP_LABEL,
    RECIPE_DIGEST_LABEL,
    RUNNER_IMAGE_LABEL,
    recipe_digest,
)
from enginery.adapters.oci.launcher import (
    EXIT_DEADLINE,
    EXIT_FAULT,
    StopSignal,
    admit_paths,
    main,
    make_tree_writable,
    run_launcher,
    write_receipt,
)
from enginery.domain.errors import MissingPrerequisiteError
from enginery.domain.runner import (
    ImageIdentity,
    LaunchSpec,
    ResourceLimits,
    RunnerProfile,
    RunnerReceipt,
    TrustMode,
    profile_violations,
)
from enginery.engine.supervisor import probe_host

from .support import (
    HOST,
    IMAGE_ID,
    INNER,
    PERMISSIONS,
    FakeDocker,
    host_probe,
    image_identity,
    make_spec,
    verify_image,
)

_NOW = datetime(2026, 10, 13, 12, 30, 5, tzinfo=UTC)


def _launch(
    spec: LaunchSpec,
    fake: FakeDocker,
    *,
    inner: Sequence[str] = INNER,
    stop: StopSignal | None = None,
    slack: int = 30,
    verify: Callable[[OciEngine, str], ImageIdentity] = verify_image,
) -> tuple[int, str]:
    stream = io.StringIO()
    code = run_launcher(
        spec,
        inner,
        engine=fake.engine(),
        verify=verify,
        host_probe=host_probe,
        clock=lambda: _NOW,
        stderr=stream,
        stop=stop,
        deadline_slack_seconds=slack,
    )
    return code, stream.getvalue()


def _receipt(spec: LaunchSpec) -> RunnerReceipt:
    return RunnerReceipt.from_state(json.loads(Path(spec.receipt_path).read_text("utf-8")))


# --- a launch that works -------------------------------------------------------------


def test_a_successful_run_qualifies_creates_checks_records_starts_waits_and_removes(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)

    code, stderr = _launch(spec, fake_docker)

    assert code == 0
    assert stderr == ""
    assert fake_docker.verbs == [
        "info",
        "network inspect",
        "network create",
        "network inspect",
        "container inspect",
        "create",
        "container inspect",
        "image inspect",
        "start",
        "wait",
        "rm",
    ]
    assert fake_docker.containers == {}
    assert fake_docker.networks == {spec.profile.network: True}


def test_the_receipt_records_what_the_engine_reported_and_is_written_before_the_start(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, env=(("ANTHROPIC_AUTH_TOKEN", "dummy-credential-4f8a91c2"),))
    receipt_present_at_start: list[bool] = []
    fake_docker.hooks["start"] = lambda _args: receipt_present_at_start.append(
        os.path.exists(spec.receipt_path)
    )

    code, stderr = _launch(spec, fake_docker)

    assert code == 0
    assert receipt_present_at_start == [True]
    receipt = _receipt(spec)
    assert receipt.operation_id == "op-1"
    assert receipt.trust_mode is TrustMode.ISOLATED_OCI
    assert receipt.profile_name == spec.profile.name
    assert receipt.profile_digest == spec.profile.digest
    assert receipt.container_name == spec.container_name
    assert receipt.engine.kind == "docker-desktop"
    assert receipt.engine.server_version == "29.8.2"
    assert receipt.image == image_identity()
    assert receipt.host == {
        "machine_id": HOST.machine_id,
        "boot_id": HOST.boot_id,
        "hostname": HOST.hostname,
    }
    assert receipt.permissions == PERMISSIONS
    assert receipt.harness_version is None and receipt.harness_protocol is None
    assert receipt.started_at == "2026-10-13T12:30:05Z"
    assert profile_violations(spec.profile, receipt.effective) == ()
    assert receipt.effective.env_keys == ("ANTHROPIC_AUTH_TOKEN",)
    # Environment keys are recorded, values never, anywhere the launcher writes.
    assert "dummy-credential-4f8a91c2" not in Path(spec.receipt_path).read_text("utf-8")
    assert "dummy-credential-4f8a91c2" not in stderr


def test_the_receipt_is_private_and_outside_the_worker_mounts(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)

    _launch(spec, fake_docker)

    receipt = Path(spec.receipt_path)
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt.parent.stat().st_mode) == 0o700
    assert receipt.parent not in (tmp_path / "workspace", tmp_path / "out")
    assert not any(path.name.startswith(".") for path in receipt.parent.iterdir())


def test_the_launcher_asks_the_engine_for_the_exact_hardened_container(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.exit_code = 0

    _launch(spec, fake_docker)

    (create,) = fake_docker.calls_of("create")
    assert create[0] == "create"
    assert create[create.index("--name") + 1] == spec.container_name
    assert create[create.index("--network") + 1] == spec.profile.network
    assert create[create.index(spec.image) :][:3] == (
        spec.image,
        "python3",
        "/opt/enginery/worker_entry.py",
    )


def test_every_engine_command_runs_under_the_engines_context_and_the_receipt_records_it(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    contexts: list[str | None] = []
    for verb in (
        "info",
        "network inspect",
        "network create",
        "container inspect",
        "create",
        "image inspect",
        "start",
        "wait",
        "rm",
    ):
        fake_docker.hooks[verb] = lambda _args: contexts.append(fake_docker.context)

    code = run_launcher(
        spec,
        INNER,
        engine=fake_docker.engine(context="lima-rootless"),
        verify=verify_image,
        host_probe=host_probe,
        stderr=io.StringIO(),
    )

    assert code == 0
    assert contexts and set(contexts) == {"lima-rootless"}
    assert len(contexts) == len(fake_docker.calls)
    assert _receipt(spec).engine.context == "lima-rootless"


def test_the_workspace_and_result_directories_are_opened_up_for_the_worker(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir()
    (workspace / "src" / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (workspace / "src" / "tool.sh").chmod(0o700)
    (workspace / "notes.txt").write_text("n", encoding="utf-8")
    (workspace / "notes.txt").chmod(0o400)
    (workspace / "src").chmod(0o700)
    (tmp_path / "out").chmod(0o700)

    code, _ = _launch(spec, fake_docker)

    assert code == 0
    assert stat.S_IMODE((workspace / "src").stat().st_mode) == 0o777
    assert stat.S_IMODE((workspace / "src" / "tool.sh").stat().st_mode) == 0o777
    assert stat.S_IMODE((workspace / "notes.txt").stat().st_mode) == 0o666
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o777
    assert stat.S_IMODE((tmp_path / "out").stat().st_mode) == 0o777


# --- exit statuses -------------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [1, 2, 7, 124, 137])
def test_a_failing_container_status_is_the_launchers_status_and_its_output_is_shown(
    fake_docker: FakeDocker, tmp_path: Path, exit_code: int
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.exit_code = exit_code
    fake_docker.log_text = "worker said: build failed\n"

    code, stderr = _launch(spec, fake_docker)

    assert code == exit_code
    assert "worker said: build failed" in stderr
    assert fake_docker.verbs[-2:] == ["logs", "rm"]
    assert fake_docker.containers == {}


def test_container_output_is_redacted_before_it_is_shown(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.exit_code = 1
    secret = "ghp_" + "a1B2" * 9
    fake_docker.log_text = f"cloning with {secret}\n"

    _, stderr = _launch(make_spec(tmp_path), fake_docker)

    assert secret not in stderr
    assert "[REDACTED:github_token]" in stderr


def test_a_successful_container_shows_no_output_and_does_not_read_its_logs(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.log_text = "noise\n"

    code, stderr = _launch(make_spec(tmp_path), fake_docker)

    assert code == 0
    assert "logs" not in fake_docker.verbs
    assert "noise" not in stderr


# --- refusing a weaker container -----------------------------------------------------


def _set_host(change: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    return lambda document: document["HostConfig"].update(change)


@pytest.mark.parametrize(
    ("tamper", "violation"),
    [
        (_set_host({"CapAdd": ["NET_ADMIN"]}), "container adds capabilities"),
        (_set_host({"Privileged": True}), "container is privileged"),
        (_set_host({"CapDrop": None}), "capabilities are not all dropped"),
        (_set_host({"ReadonlyRootfs": False}), "root filesystem is writable"),
        (_set_host({"Memory": 0}), "memory limit is not the profile limit"),
        (_set_host({"MemorySwap": -1}), "swap is not disabled"),
        (_set_host({"NanoCpus": 0}), "cpu limit is not the profile limit"),
        (_set_host({"PidsLimit": None}), "pids limit is not the profile limit"),
        (_set_host({"SecurityOpt": None}), "security option 'no-new-privileges' is not set"),
        (_set_host({"NetworkMode": "bridge"}), "not attached only to the internal runner network"),
        (
            _set_host({"Devices": [{"PathOnHost": "/dev/kvm", "PathInContainer": "/dev/kvm"}]}),
            "host devices",
        ),
        (_set_host({"PidMode": "host"}), "shares a pid namespace"),
        (_set_host({"IpcMode": "host"}), "ipc mode is 'host'"),
        (_set_host({"VolumesFrom": ["docker-socket-holder"]}), "volumes from another container"),
        (
            lambda document: document["Config"].update(User="0:0"),
            "user is '0:0'",
        ),
        (
            lambda document: document["Mounts"].append(
                {"Type": "bind", "Destination": "/home/operator", "RW": True}
            ),
            "unadmitted mount bind at /home/operator",
        ),
        (
            lambda document: document["Config"]["Env"].append("GH_TOKEN=ghp_not_allowed"),
            "environment keys outside the allowlist: ['GH_TOKEN']",
        ),
        (
            lambda document: document.update(Image="sha256:" + "9" * 64),
            "not created from the pinned image id",
        ),
    ],
)
def test_a_container_weaker_than_the_profile_is_removed_and_never_started_or_recorded(
    fake_docker: FakeDocker,
    tmp_path: Path,
    tamper: Callable[[dict[str, Any]], None],
    violation: str,
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.tamper = tamper

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert violation in stderr
    assert "profile violation" in stderr
    assert "start" not in fake_docker.verbs
    assert "wait" not in fake_docker.verbs
    assert fake_docker.verbs[-1] == "rm"
    assert fake_docker.containers == {}
    assert not os.path.lexists(spec.receipt_path)


def test_a_receipt_from_an_earlier_attempt_never_survives_a_refused_attempt(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    Path(spec.receipt_path).parent.mkdir(parents=True)
    Path(spec.receipt_path).write_text("a receipt of a dead attempt", encoding="utf-8")
    fake_docker.tamper = _set_host({"Privileged": True})

    code, _ = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert not os.path.lexists(spec.receipt_path)


def test_create_failure_and_create_warnings_refuse_the_launch(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.failures["create"] = (125, "docker: Error response from daemon: boom")

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "could not create the container" in stderr
    assert "boom" in stderr
    assert "start" not in fake_docker.verbs


def test_an_engine_that_warns_while_creating_is_refused_because_limits_may_not_hold(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.create_stderr = "WARNING: Your kernel does not support swap limit capabilities\n"

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "swap limit capabilities" in stderr
    assert "start" not in fake_docker.verbs
    assert fake_docker.containers == {}
    assert not os.path.lexists(spec.receipt_path)


# --- refusing before the engine is touched -------------------------------------------


def test_an_engine_that_cannot_enforce_the_profile_is_refused_with_the_requirement_named(
    tmp_path: Path,
) -> None:
    fake = FakeDocker(info={**FakeDocker().info, "CgroupVersion": "1", "PidsLimit": False})

    code, stderr = _launch(make_spec(tmp_path), fake)

    assert code == EXIT_FAULT
    assert "cgroup v2" in stderr
    assert "pids limit" in stderr
    assert fake.verbs == ["info"]


def test_an_unreachable_engine_is_a_launcher_fault_not_a_worker_failure(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.unreachable = True

    code, stderr = _launch(make_spec(tmp_path), fake_docker)

    assert code == EXIT_FAULT
    assert "unreachable" in stderr


def test_an_image_that_fails_verification_is_refused_before_anything_is_created(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    def refuse(_engine: OciEngine, image_id: str) -> ImageIdentity:
        raise MissingPrerequisiteError(f"runner image {image_id} was built from another recipe")

    code, stderr = _launch(make_spec(tmp_path), fake_docker, verify=refuse)

    assert code == EXIT_FAULT
    assert "another recipe" in stderr
    assert fake_docker.verbs == ["info"]


def test_a_verifier_that_answers_for_another_image_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    def other(_engine: OciEngine, _image_id: str) -> ImageIdentity:
        return image_identity("sha256:" + "7" * 64)

    code, stderr = _launch(make_spec(tmp_path), fake_docker, verify=other)

    assert code == EXIT_FAULT
    assert "not the pinned image" in stderr
    assert "create" not in fake_docker.verbs


def test_an_internal_network_that_is_not_internal_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.networks[spec.profile.network] = False

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "not internal" in stderr
    assert "create" not in fake_docker.verbs


def test_an_environment_key_outside_the_allowlist_is_refused_without_contacting_the_engine(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    object.__setattr__(spec, "env", (("GH_TOKEN", "ghp_" + "z" * 36),))

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "allowlist" in stderr
    assert "ghp_" not in stderr
    assert fake_docker.calls == []


# --- host paths ----------------------------------------------------------------------


def _expect_refusal(spec: LaunchSpec, fake: FakeDocker, message: str) -> None:
    code, stderr = _launch(spec, fake)

    assert code == EXIT_FAULT
    assert message in stderr
    assert fake.calls == []
    assert not os.path.lexists(spec.receipt_path)


def test_a_symlinked_workspace_is_refused(fake_docker: FakeDocker, tmp_path: Path) -> None:
    real = tmp_path / "real-workspace"
    real.mkdir()
    spec = make_spec(tmp_path)
    (tmp_path / "workspace").rmdir()
    (tmp_path / "workspace").symlink_to(real)

    _expect_refusal(spec, fake_docker, "workspace directory must not be a symlink")


def test_a_symlinked_result_directory_is_refused(fake_docker: FakeDocker, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    spec = make_spec(tmp_path)
    (tmp_path / "out").rmdir()
    (tmp_path / "out").symlink_to(real)

    _expect_refusal(spec, fake_docker, "result directory must not be a symlink")


def test_a_missing_or_non_directory_workspace_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    (tmp_path / "workspace").rmdir()
    _expect_refusal(spec, fake_docker, "workspace directory is not accessible")

    (tmp_path / "workspace").write_text("a file", encoding="utf-8")
    _expect_refusal(spec, fake_docker, "workspace path is not a directory")


@pytest.mark.parametrize(
    "receipt",
    ["workspace/receipt.json", "out/receipt.json", "workspace/deep/er/receipt.json"],
)
def test_a_receipt_inside_a_worker_mount_is_refused(
    fake_docker: FakeDocker, tmp_path: Path, receipt: str
) -> None:
    spec = make_spec(tmp_path, receipt_path=str(tmp_path / receipt))

    _expect_refusal(spec, fake_docker, "the receipt path must be outside")


def test_a_receipt_path_that_is_a_worker_mount_itself_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, receipt_path=str(tmp_path / "workspace"))

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "the receipt path must be outside" in stderr
    assert fake_docker.calls == []
    assert (tmp_path / "workspace").is_dir()  # refused before anything was removed


def test_a_receipt_reached_through_a_symlinked_directory_is_still_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "workspace", target_is_directory=True)
    spec = make_spec(tmp_path, receipt_path=str(tmp_path / "alias" / "receipt.json"))

    _expect_refusal(spec, fake_docker, "the receipt path must be outside")


def test_overlapping_workspace_and_result_directories_are_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    make_spec(tmp_path)  # creates the workspace and result directories
    nested = tmp_path / "workspace" / "out"
    nested.mkdir()
    spec = make_spec(tmp_path, out_path=str(nested))

    _expect_refusal(spec, fake_docker, "must not overlap")


def test_admit_paths_accepts_a_clean_layout(tmp_path: Path) -> None:
    admit_paths(make_spec(tmp_path))


def test_write_receipt_replaces_atomically_with_a_private_file_and_leaves_no_temporary(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    _launch(spec, fake_docker)
    path = Path(spec.receipt_path)
    replacement = dataclasses.replace(_receipt(spec), started_at="2030-01-01T00:00:00Z")

    write_receipt(spec.receipt_path, replacement)

    assert _receipt(spec).started_at == "2030-01-01T00:00:00Z"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


# --- opening the tree up ---------------------------------------------------------------


def test_make_tree_writable_is_chmod_a_plus_rwx_and_never_follows_a_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "host-secret.txt"
    secret.write_text("operator data", encoding="utf-8")
    secret.chmod(0o600)
    outside.chmod(0o700)
    tree = tmp_path / "tree"
    (tree / "sub" / "deeper").mkdir(parents=True)
    plain = tree / "sub" / "plain.txt"
    plain.write_text("p", encoding="utf-8")
    plain.chmod(0o600)
    script = tree / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)
    readonly = tree / "sub" / "deeper" / "ro.txt"
    readonly.write_text("r", encoding="utf-8")
    readonly.chmod(0o444)
    (tree / "link-to-file").symlink_to(secret)
    (tree / "link-to-dir").symlink_to(outside, target_is_directory=True)
    (tree / "dangling").symlink_to(tmp_path / "does-not-exist")
    fifo = tree / "pipe"
    os.mkfifo(fifo, 0o600)
    tree.chmod(0o750)
    (tree / "sub").chmod(0o700)

    make_tree_writable(str(tree))

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.lstat().st_mode)

    assert mode(tree) == 0o777
    assert mode(tree / "sub") == 0o777
    assert mode(tree / "sub" / "deeper") == 0o777
    assert mode(plain) == 0o666  # no execute bit anywhere: X adds none
    assert mode(readonly) == 0o666
    assert mode(script) == 0o777  # had an execute bit: X grants execute to everyone
    assert mode(fifo) == 0o600  # not a regular file or directory: left alone
    # Nothing a symlink points at was touched, and symlinks were not descended into.
    assert mode(secret) == 0o600
    assert mode(outside) == 0o700


def test_make_tree_writable_reports_a_directory_it_cannot_read(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read every directory")
    tree = tmp_path / "tree"
    locked = tree / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        with pytest.raises(OSError):
            make_tree_writable(str(tree))
    finally:
        locked.chmod(0o700)


# --- idempotency ---------------------------------------------------------------------


def test_a_container_left_by_an_earlier_attempt_of_the_same_operation_is_replaced(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.create_worker(spec)
    fake_docker.calls.clear()

    code, stderr = _launch(spec, fake_docker)

    assert code == 0
    assert "left by an earlier attempt" in stderr
    assert spec.container_name in stderr
    verbs = fake_docker.verbs
    assert verbs.index("rm") < verbs.index("create")
    assert verbs.count("create") == 1


# --- signals and deadlines -----------------------------------------------------------


def _stop_after_start(fake: FakeDocker, stop: StopSignal, number: int) -> None:
    fake.hold_running = True
    fake.hooks["start"] = lambda _args: threading.Timer(0.2, stop.request, (number,)).start()


def _wait_for_waiter_threads_to_finish() -> None:
    """The thread blocked in ``docker wait`` ends once its container is killed and removed."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name == "oci-wait" and t.is_alive()]:
            return
        time.sleep(0.02)
    pytest.fail("the thread waiting on the container never finished")


@pytest.mark.parametrize(("number", "expected"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_stop_signal_kills_then_removes_the_container_and_exits_128_plus_the_signal(
    fake_docker: FakeDocker, tmp_path: Path, number: signal.Signals, expected: int
) -> None:
    spec = make_spec(tmp_path)
    stop = StopSignal()
    _stop_after_start(fake_docker, stop, number)

    code, stderr = _launch(spec, fake_docker, stop=stop)

    assert code == expected
    verbs = fake_docker.verbs
    assert verbs.index("start") < verbs.index("kill") < verbs.index("rm")
    assert verbs[-1] == "rm"
    (kill,) = fake_docker.calls_of("kill")
    assert kill == ("kill", "--signal", "KILL", spec.container_name)
    assert fake_docker.containers == {}
    assert f"stopped by signal {int(number)}" in stderr
    assert os.path.exists(spec.receipt_path)
    _wait_for_waiter_threads_to_finish()


def test_a_stop_requested_before_anything_started_touches_no_container(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    stop = StopSignal()
    stop.request(signal.SIGTERM)

    code, _ = _launch(make_spec(tmp_path), fake_docker, stop=stop)

    assert code == 143
    assert fake_docker.calls == []


def test_a_stop_that_arrives_while_the_container_is_created_but_not_started_removes_it(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    stop = StopSignal()
    fake_docker.hooks["create"] = lambda _args: stop.request(signal.SIGTERM)

    code, _ = _launch(spec, fake_docker, stop=stop)

    assert code == 143
    assert "start" not in fake_docker.verbs
    assert fake_docker.verbs[-1] == "rm"
    assert fake_docker.containers == {}
    assert not os.path.lexists(spec.receipt_path)


def test_the_hard_deadline_kills_then_removes_the_container_and_exits_124(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    profile = RunnerProfile(resources=ResourceLimits(stop_grace_seconds=1))
    spec = make_spec(tmp_path, profile=profile, time_budget_seconds=1)
    fake_docker.hold_running = True
    fake_docker.log_text = "still working\n"

    started = time.monotonic()
    code, stderr = _launch(spec, fake_docker, slack=0)

    assert code == EXIT_DEADLINE
    assert 1.9 <= time.monotonic() - started < 15
    verbs = fake_docker.verbs
    assert verbs.index("kill") < verbs.index("logs") < verbs.index("rm")
    assert "hard deadline of 2s" in stderr
    assert "still working" in stderr
    assert fake_docker.containers == {}


@pytest.mark.parametrize(("budget", "grace", "slack", "total"), [(1, 1, 0, 2), (1, 1, 1, 3)])
def test_the_hard_deadline_is_the_budget_plus_the_stop_grace_plus_the_slack(
    fake_docker: FakeDocker, tmp_path: Path, budget: int, grace: int, slack: int, total: int
) -> None:
    profile = RunnerProfile(resources=ResourceLimits(stop_grace_seconds=grace))
    spec = make_spec(tmp_path, profile=profile, time_budget_seconds=budget)
    fake_docker.hold_running = True

    started = time.monotonic()
    code, stderr = _launch(spec, fake_docker, slack=slack)

    assert code == EXIT_DEADLINE
    assert f"hard deadline of {total}s" in stderr
    assert total - 0.1 <= time.monotonic() - started < total + 10


def test_a_container_that_exits_on_its_own_never_reports_a_deadline(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.exit_code = 1

    _, stderr = _launch(make_spec(tmp_path), fake_docker)

    assert "hard deadline" not in stderr


def test_a_failing_wait_is_a_launcher_fault_and_still_removes_the_container(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.failures["wait"] = (1, "Error response from daemon: connection reset")

    code, stderr = _launch(make_spec(tmp_path), fake_docker)

    assert code == EXIT_FAULT
    assert "connection reset" in stderr
    assert fake_docker.verbs[-1] == "rm"
    assert fake_docker.containers == {}


def test_a_container_that_cannot_be_removed_is_reported_but_keeps_the_workers_status(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.failures["rm"] = (1, "Error response from daemon: device or resource busy")
    fake_docker.exit_code = 3

    code, stderr = _launch(make_spec(tmp_path), fake_docker)

    assert code == 3
    assert "could not be removed" in stderr
    assert "device or resource busy" in stderr


def test_stop_signal_installs_for_sigterm_and_sigint_and_restores_the_previous_handlers() -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    stop = StopSignal()

    with stop.installed():
        signal.raise_signal(signal.SIGTERM)
        assert stop.number == signal.SIGTERM
        signal.raise_signal(signal.SIGINT)
        assert stop.number == signal.SIGTERM  # the first request wins
        assert stop.wake.is_set()

    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


# --- the command line ----------------------------------------------------------------


def _labelled_fake() -> FakeDocker:
    fake = FakeDocker()
    fake.images[IMAGE_ID]["Config"]["Labels"] = {
        RUNNER_IMAGE_LABEL: "1",
        CLAUDE_CODE_LABEL: "2.1.296",
        OMP_LABEL: "18.10.3",
        BUN_LABEL: "1.3.14",
        RECIPE_DIGEST_LABEL: recipe_digest(),
    }
    return fake


def test_main_parses_the_spec_and_runs_it_through_the_real_image_verifier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _labelled_fake()
    spec = make_spec(tmp_path)
    fake.exit_code = 9

    code = main(
        ["--spec", json.dumps(spec.to_state()), "--", *INNER],
        engine=fake.engine(),
        host_probe=host_probe,
    )

    assert code == 9
    receipt = _receipt(spec)
    assert receipt.image.recipe_digest == recipe_digest()
    assert dict(receipt.image.harness_versions) == {
        "bun": "1.3.14",
        "claude-code": "2.1.296",
        "omp": "18.10.3",
    }
    assert receipt.host["machine_id"] == HOST.machine_id
    assert receipt.host["boot_id"] == HOST.boot_id
    (create,) = fake.calls_of("create")
    assert create[create.index("--") + 1 :] == INNER
    assert capsys.readouterr().out == ""


def test_main_refuses_an_image_not_built_from_this_recipe(tmp_path: Path) -> None:
    fake = _labelled_fake()
    fake.images[IMAGE_ID]["Config"]["Labels"][RECIPE_DIGEST_LABEL] = "sha256:" + "0" * 64
    spec = make_spec(tmp_path)

    code = main(["--spec", json.dumps(spec.to_state()), "--", *INNER], engine=fake.engine())

    assert code == EXIT_FAULT
    assert "create" not in fake.verbs


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--spec", "{}"],
        ["--", "omp"],
        ["--spec", "not json", "--", "omp"],
        ["--spec", "{}", "--", "omp"],
        ["--spec", "[]", "--", "omp"],
        ["--other", "{}", "--", "omp"],
        ["--spec", "{}", "extra", "--", "omp"],
    ],
)
def test_main_refuses_a_malformed_command_line_without_touching_the_engine(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeDocker()

    assert main(arguments, engine=fake.engine()) == EXIT_FAULT

    assert fake.calls == []
    assert capsys.readouterr().err.startswith("launcher: ")


def test_main_refuses_a_spec_without_a_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = make_spec(tmp_path)

    code = main(["--spec", json.dumps(spec.to_state()), "--"], engine=FakeDocker().engine())

    assert code == EXIT_FAULT
    assert "needs a command" in capsys.readouterr().err


# --- a real engine -------------------------------------------------------------------

_STANDIN_IDENTITY = "sha256:" + "5" * 64


def _standin_verifier(_engine: OciEngine, image_id: str) -> ImageIdentity:
    """TEST-ONLY: the stand-in image carries no recipe labels, so it cannot pass the real
    verifier; the launcher is given an identity for it instead."""
    return ImageIdentity(image_id=image_id, recipe_digest=_STANDIN_IDENTITY)


def _real_spec(
    root: Path,
    operation: str,
    standin_image: str,
    profile: RunnerProfile,
    **overrides: Any,
) -> LaunchSpec:
    return make_spec(
        root, operation_id=operation, image=standin_image, profile=profile, **overrides
    )


def test_real_launch_runs_the_worker_records_the_receipt_and_removes_the_container(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    (real_root / "workspace" / "from-host.txt").write_text("host file", encoding="utf-8")
    (real_root / "workspace" / "from-host.txt").chmod(0o600)
    script = (
        "import os\n"
        "open('/workspace/from-worker.txt', 'w').write('uid=%d' % os.getuid())\n"
        "print(open('/workspace/from-host.txt').read(), sorted(os.environ)[:0])\n"
    )
    stream = io.StringIO()

    code = run_launcher(
        spec,
        ("python3", "-c", script),
        engine=real_engine,
        verify=_standin_verifier,
        stderr=stream,
    )

    assert code == 0, stream.getvalue()
    result = json.loads((real_root / "out" / "result.json").read_text("utf-8"))
    assert result["terminal_status"] == "succeeded"
    assert "host file" in result["output"]
    assert (real_root / "workspace" / "from-worker.txt").read_text() == "uid=65532"
    receipt = _receipt(spec)
    assert receipt.engine == real_engine.qualify()
    assert receipt.image.image_id == standin_image
    assert receipt.host["machine_id"] == probe_host().machine_id
    assert profile_violations(real_profile, receipt.effective) == ()
    assert stat.S_IMODE(Path(spec.receipt_path).stat().st_mode) == 0o600
    assert real_engine.container_exists(spec.container_name) is False
    # The host can delete everything the worker made.
    (real_root / "workspace" / "from-worker.txt").unlink()
    (real_root / "out" / "result.json").unlink()


def test_real_launch_returns_the_workers_exit_status(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)

    code = run_launcher(
        spec,
        ("python3", "-c", "import sys; print('about to fail'); sys.exit(5)"),
        engine=real_engine,
        verify=_standin_verifier,
        stderr=io.StringIO(),
    )

    assert code == 5
    result = json.loads((real_root / "out" / "result.json").read_text("utf-8"))
    assert result["terminal_status"] == "failed"
    assert "about to fail" in result["output"]
    assert real_engine.container_exists(spec.container_name) is False


def test_real_launch_refuses_an_engine_that_would_attach_a_container_to_an_open_network(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    created = real_engine.run_cli(("network", "create", real_profile.network))
    assert created.returncode == 0, created.stderr
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    stream = io.StringIO()

    code = run_launcher(spec, INNER, engine=real_engine, verify=_standin_verifier, stderr=stream)

    assert code == EXIT_FAULT
    assert "not internal" in stream.getvalue()
    assert real_engine.container_exists(spec.container_name) is False
    assert not os.path.lexists(spec.receipt_path)


def test_real_hard_deadline_kills_a_worker_that_ignores_its_own_deadline(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(
        real_root, real_operation(), standin_image, real_profile, time_budget_seconds=1
    )
    stubborn = (
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"
    )
    stream = io.StringIO()

    started = time.monotonic()
    code = run_launcher(
        spec,
        ("python3", "-c", stubborn),
        engine=real_engine,
        verify=_standin_verifier,
        stderr=stream,
        deadline_slack_seconds=0,
    )

    assert code == EXIT_DEADLINE, stream.getvalue()
    assert time.monotonic() - started < 60
    assert "hard deadline" in stream.getvalue()
    assert real_engine.container_exists(spec.container_name) is False


_DRIVER = """
import json, sys
from enginery.adapters.oci.launcher import main
from enginery.adapters.oci.engine import OciEngine
from enginery.domain.runner import ImageIdentity

def verify(engine, image_id):
    return ImageIdentity(image_id=image_id, recipe_digest="sha256:" + "5" * 64)

spec_json, *inner = sys.argv[1:]
raise SystemExit(main(["--spec", spec_json, "--", *inner], verify=verify))
"""


@pytest.mark.parametrize(("number", "expected"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_real_signal_to_the_launcher_process_kills_and_removes_the_worker_container(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
    number: signal.Signals,
    expected: int,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _DRIVER,
            json.dumps(spec.to_state()),
            "python3",
            "-c",
            "import time; time.sleep(120)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        running = False
        while time.monotonic() < deadline and process.poll() is None:
            try:
                state = real_engine.container_inspect(spec.container_name)["State"]
            except Exception:
                state = {}
            if state.get("Running") is True:
                running = True
                break
            time.sleep(0.3)
        assert running, "the worker container never started"
        assert os.path.exists(spec.receipt_path)

        process.send_signal(number)
        _, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == expected, stderr
    assert real_engine.container_exists(spec.container_name) is False
