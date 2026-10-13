"""The launcher's host-side boundary: outcome record, stale state, mapping, bounds, orphans.

The first half runs against the in-memory engine; the second half (``test_real_*``) runs the
same boundaries against a real engine with the TEST-ONLY stand-in worker image.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import (
    BUNDLE_FILE_NAME,
    create_arguments,
    max_file_bytes,
    resource_bounds,
    store_names_for,
)
from enginery.adapters.oci.launcher import (
    EXIT_DEADLINE,
    EXIT_FAULT,
    StopSignal,
    run_launcher,
)
from enginery.adapters.oci.reconcile import reconcile_runner_containers
from enginery.adapters.runner_launch import (
    bind_result_to_attempt,
    read_bounded_text,
    read_launch_outcome,
    require_exited,
)
from enginery.domain.errors import WorkerFailureError
from enginery.domain.runner import (
    DISK_BOUND_KERNEL_VOLUME,
    ImageIdentity,
    LaunchOutcome,
    LaunchSpec,
    OutcomeState,
    PermissionMapping,
    ResourceLimits,
    RunnerProfile,
    RunnerReceipt,
    outcome_path_for,
)

from .support import (
    INNER,
    PERMISSIONS,
    PERMISSIONS_WITHOUT_ARGUMENTS,
    FakeDocker,
    host_probe,
    make_spec,
    verify_image,
)


def _run(
    spec: LaunchSpec,
    fake: FakeDocker,
    *,
    inner: tuple[str, ...] = INNER,
    stop: StopSignal | None = None,
    slack: int = 30,
) -> tuple[int, str]:
    stream = io.StringIO()
    code = run_launcher(
        spec,
        inner,
        engine=fake.engine(),
        verify=verify_image,
        host_probe=host_probe,
        stderr=stream,
        stop=stop,
        deadline_slack_seconds=slack,
    )
    return code, stream.getvalue()


def _outcome(spec: LaunchSpec) -> LaunchOutcome:
    path = outcome_path_for(spec.receipt_path)
    return LaunchOutcome.from_state(json.loads(Path(path).read_text("utf-8")))


def _nonce_given_to_the_worker(fake: FakeDocker) -> str:
    *_, create = fake.calls_of("create")  # the worker is created after the store container
    return create[create.index("--nonce") + 1]


# --- the launcher's own outcome ----------------------------------------------------------


def test_the_launcher_records_its_outcome_and_the_nonce_it_gave_the_worker(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)

    code, _ = _run(spec, fake_docker)

    assert code == 0
    outcome = _outcome(spec)
    assert outcome.state is OutcomeState.EXITED
    assert outcome.exit_code == 0
    assert outcome.operation_id == spec.operation_id
    assert outcome.nonce == _nonce_given_to_the_worker(fake_docker)
    assert len(outcome.nonce) >= 32
    path = Path(outcome_path_for(spec.receipt_path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent == Path(spec.receipt_path).parent
    assert not path.is_relative_to(tmp_path / "workspace")
    assert not path.is_relative_to(tmp_path / "out")


def test_every_launch_mints_a_new_nonce(fake_docker: FakeDocker, tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    nonces = []
    for _ in range(3):
        _run(spec, fake_docker)
        nonces.append(_outcome(spec).nonce)
        fake_docker.calls.clear()

    assert len(set(nonces)) == 3


@pytest.mark.parametrize("exit_code", [1, 124, 125, 126, 137])
def test_a_container_exit_status_is_recorded_as_an_exit_whatever_the_number(
    fake_docker: FakeDocker, tmp_path: Path, exit_code: int
) -> None:
    """124 and 125 are the launcher's own statuses; a worker that exits with them is still just
    a container that exited, and the outcome says so."""
    spec = make_spec(tmp_path)
    fake_docker.exit_code = exit_code

    code, _ = _run(spec, fake_docker)

    assert code == exit_code
    outcome = _outcome(spec)
    assert outcome.state is OutcomeState.EXITED
    assert outcome.exit_code == exit_code
    assert require_exited(outcome, what="worker") == exit_code


def test_a_refused_launch_is_recorded_with_the_violations_found(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.tamper = lambda document: document["HostConfig"].update(Privileged=True)

    code, _ = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    outcome = _outcome(spec)
    assert outcome.state is OutcomeState.REFUSED
    assert outcome.exit_code is None
    assert "container is privileged" in outcome.violations
    with pytest.raises(WorkerFailureError, match="refused to launch"):
        require_exited(outcome, what="worker")


def test_an_engine_fault_is_recorded_as_a_fault(fake_docker: FakeDocker, tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    fake_docker.unreachable = True

    code, _ = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert _outcome(spec).state is OutcomeState.FAULTED


def test_a_deadline_is_recorded_and_is_not_an_exit(fake_docker: FakeDocker, tmp_path: Path) -> None:
    profile = RunnerProfile(resources=ResourceLimits(stop_grace_seconds=1))
    spec = make_spec(tmp_path, profile=profile, time_budget_seconds=1)
    fake_docker.hold_running = True

    code, _ = _run(spec, fake_docker, slack=0)

    assert code == EXIT_DEADLINE
    outcome = _outcome(spec)
    assert outcome.state is OutcomeState.DEADLINE
    assert outcome.exit_code is None


def test_a_stop_is_recorded_as_an_interruption(fake_docker: FakeDocker, tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    stop = StopSignal()
    fake_docker.hold_running = True
    fake_docker.hooks["start"] = lambda _args: threading.Timer(
        0.1, stop.request, (signal.SIGTERM,)
    ).start()

    code, _ = _run(spec, fake_docker, stop=stop)

    assert code == 143
    assert _outcome(spec).state is OutcomeState.INTERRUPTED


def test_the_outcome_says_running_while_the_container_runs(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    """A launcher that is lost mid-run leaves "running", which collection rejects."""
    spec = make_spec(tmp_path)
    seen: list[OutcomeState] = []
    fake_docker.hooks["wait"] = lambda _args: seen.append(_outcome(spec).state)

    _run(spec, fake_docker)

    assert seen == [OutcomeState.RUNNING]
    running = LaunchOutcome(
        operation_id=spec.operation_id, nonce="n" * 32, state=OutcomeState.RUNNING
    )
    with pytest.raises(WorkerFailureError, match="did not finish"):
        require_exited(running, what="worker")


def test_a_launch_refused_at_admission_leaves_no_outcome_inside_a_worker_mount(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, receipt_path=str(tmp_path / "workspace" / "receipt.json"))

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "the receipt path must be outside" in stderr
    assert list((tmp_path / "workspace").iterdir()) == []
    assert fake_docker.calls == []


def test_the_receipt_records_the_bound_mechanism_and_the_harness_identity(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, harness_version="2.1.4", harness_protocol="a" * 64)

    _run(spec, fake_docker)

    receipt = RunnerReceipt.from_state(json.loads(Path(spec.receipt_path).read_text("utf-8")))
    assert receipt.harness_version == "2.1.4"
    assert receipt.harness_protocol == "a" * 64
    assert receipt.bounds is not None
    assert receipt.bounds.disk_mechanism == DISK_BOUND_KERNEL_VOLUME == "kernel-capped-volume"
    assert not hasattr(receipt.bounds, "poll_seconds")  # nothing on the host measures a kernel cap
    assert receipt.bounds == resource_bounds(spec.profile)
    assert receipt.bounds.workspace_bytes == 4096 * 1024 * 1024
    assert receipt.bounds.out_bytes == 1024 * 1024 * 1024
    assert receipt.bounds.workspace_inodes == 500_000
    assert receipt.bounds.out_inodes == 100_000
    assert receipt.bounds.log_driver == "json-file"
    assert receipt.bounds.log_max_size == "1m"
    assert receipt.bounds.log_max_file == 2
    assert receipt.bounds.max_file_bytes == max_file_bytes(spec.profile)


def test_a_receipt_without_bounds_keeps_its_digest(fake_docker: FakeDocker, tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    _run(spec, fake_docker)
    receipt = RunnerReceipt.from_state(json.loads(Path(spec.receipt_path).read_text("utf-8")))
    legacy = dataclasses.replace(receipt, bounds=None)

    assert "bounds" not in legacy.to_state()
    assert RunnerReceipt.from_state(legacy.to_state()).digest == legacy.digest
    assert receipt.digest != legacy.digest


# --- nothing left by an earlier attempt can be mistaken for this one ---------------------


def _plant_stale_attempt(spec: LaunchSpec) -> str:
    """An earlier attempt's outcome, receipt, result and bundle, all internally consistent."""
    old_nonce = "o" * 32
    out = Path(spec.out_path)
    (out / spec.result_name).write_text(
        json.dumps({"operation_id": spec.operation_id, "output": "OLD", "nonce": old_nonce}),
        encoding="utf-8",
    )
    (out / BUNDLE_FILE_NAME).write_bytes(b"old bundle")
    receipts = Path(spec.receipt_path).parent
    receipts.mkdir(parents=True, exist_ok=True)
    Path(spec.receipt_path).write_text("{}", encoding="utf-8")
    Path(outcome_path_for(spec.receipt_path)).write_text(
        json.dumps(
            LaunchOutcome(
                operation_id=spec.operation_id,
                nonce=old_nonce,
                state=OutcomeState.EXITED,
                exit_code=0,
            ).to_state()
        ),
        encoding="utf-8",
    )
    return old_nonce


def test_a_stale_result_and_bundle_are_removed_before_the_container_starts(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    old_nonce = _plant_stale_attempt(spec)
    at_start: list[tuple[bool, bool, str]] = []
    out = Path(spec.out_path)
    fake_docker.hooks["start"] = lambda args: (
        at_start.append(
            (
                os.path.lexists(out / spec.result_name),
                os.path.lexists(out / BUNDLE_FILE_NAME),
                _outcome(spec).nonce,
            )
        )
        if args[1] == spec.container_name
        else None
    )

    code, _ = _run(spec, fake_docker)

    assert code == 0
    ((result_present, bundle_present, nonce_at_start),) = at_start
    assert (result_present, bundle_present) == (False, False)
    assert nonce_at_start != old_nonce


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_a_stale_result_of_any_kind_is_removed_and_never_followed(
    fake_docker: FakeDocker, tmp_path: Path, kind: str
) -> None:
    spec = make_spec(tmp_path)
    result = Path(spec.out_path) / spec.result_name
    bundle = Path(spec.out_path) / BUNDLE_FILE_NAME
    secret = tmp_path / "host-secret.txt"
    secret.write_text("host secret", encoding="utf-8")
    if kind == "symlink":
        result.symlink_to(secret)
        bundle.symlink_to(secret)
    elif kind == "fifo":
        os.mkfifo(result)
        os.mkfifo(bundle)
    else:
        result.mkdir()
        (result / "inner").write_text("x", encoding="utf-8")
        bundle.mkdir()
    present: list[tuple[bool, bool]] = []
    fake_docker.hooks["start"] = lambda args: (
        present.append((os.path.lexists(result), os.path.lexists(bundle)))
        if args[1] == spec.container_name
        else None
    )

    code, _ = _run(spec, fake_docker)

    assert code == 0
    assert present == [(False, False)]
    assert secret.read_text("utf-8") == "host secret"


def test_a_stale_outcome_never_survives_a_refused_attempt(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    _plant_stale_attempt(spec)
    fake_docker.tamper = lambda document: document["HostConfig"].update(Privileged=True)

    _run(spec, fake_docker)

    assert _outcome(spec).state is OutcomeState.REFUSED


def test_a_stale_outcome_never_survives_a_refused_admission(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    _plant_stale_attempt(spec)
    workspace = tmp_path / "workspace"
    workspace.rmdir()
    workspace.symlink_to(tmp_path / "out", target_is_directory=True)

    code, _ = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert not os.path.lexists(outcome_path_for(spec.receipt_path))
    assert not os.path.lexists(spec.receipt_path)


def test_a_stale_result_replayed_into_a_new_attempt_is_not_bound_to_it(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    """Even a stale result that survived (it cannot, above) fails the nonce binding."""
    spec = make_spec(tmp_path)
    _run(spec, fake_docker)
    new_nonce = _outcome(spec).nonce
    stale = json.dumps({"operation_id": spec.operation_id, "output": "OLD", "nonce": "o" * 32})

    with pytest.raises(WorkerFailureError, match="not bound to this attempt"):
        bind_result_to_attempt(stale, nonce=new_nonce, what="worker result")


# --- the permission mapping -------------------------------------------------------------


def test_a_bypass_permission_mapping_is_refused_before_the_engine_is_contacted(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    bypass = PermissionMapping(
        harness="omp",
        name="omp-bypass",
        arguments=(),
        unconditional_bypass=True,
        rationale="everything allowed",
    )
    spec = make_spec(tmp_path, permissions=bypass)

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "turns the harness's permission checks off" in stderr
    assert fake_docker.calls == []
    assert _outcome(spec).state is OutcomeState.REFUSED


@pytest.mark.parametrize(
    "inner",
    [
        ("omp", "-p", "do the thing"),
        ("omp", "--approval", "-p", "ask"),
        ("omp", "--approval", "-p", "do it", "ask"),
        ("omp", "ask", "--approval", "-p", "do it"),
    ],
    ids=["absent", "split", "scattered", "reordered"],
)
def test_a_command_that_does_not_carry_the_mappings_arguments_is_refused(
    fake_docker: FakeDocker, tmp_path: Path, inner: tuple[str, ...]
) -> None:
    spec = make_spec(tmp_path, permissions=PERMISSIONS)

    code, stderr = _run(spec, fake_docker, inner=inner)

    assert code == EXIT_FAULT
    assert "does not carry the arguments of permission mapping" in stderr
    assert fake_docker.calls == []


def test_a_command_that_carries_the_mappings_arguments_in_order_is_accepted(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, permissions=PERMISSIONS)

    code, _ = _run(spec, fake_docker, inner=("omp", "-p", "x", "--approval", "ask", "--mode"))

    assert code == 0


def test_a_mapping_without_arguments_asks_nothing_of_the_command(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, permissions=PERMISSIONS_WITHOUT_ARGUMENTS)

    code, _ = _run(spec, fake_docker, inner=("pytest", "-q"))

    assert code == 0


# --- the disk bound: kernel-capped volumes -----------------------------------------------


def test_the_workspace_and_result_trees_are_volumes_capped_at_the_profiles_numbers(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    profile = RunnerProfile(
        resources=ResourceLimits(
            workspace_mib=8, out_mib=2, workspace_inodes=64, out_inodes=16, memory_mib=256
        )
    )
    spec = make_spec(tmp_path, profile=profile)
    names = store_names_for(spec.container_name)
    held: dict[str, dict[str, Any]] = {}
    fake_docker.hooks["start"] = lambda args: (
        held.update(json.loads(json.dumps(fake_docker.volumes)))
        if args[1] == spec.container_name
        else None
    )

    code, _ = _run(spec, fake_docker)

    assert code == 0
    assert held[names.workspace_volume]["Options"] == {
        "type": "tmpfs",
        "device": "tmpfs",
        "o": "size=8m,nr_inodes=64,uid=65532,gid=65532,mode=0700,nosuid,nodev",
    }
    assert held[names.out_volume]["Options"]["o"] == (
        "size=2m,nr_inodes=16,uid=65532,gid=65532,mode=0700,nosuid,nodev,noexec"
    )
    bounds = RunnerReceipt.from_state(json.loads(Path(spec.receipt_path).read_text())).bounds
    assert bounds is not None
    assert bounds.disk_mechanism == DISK_BOUND_KERNEL_VOLUME
    assert (bounds.workspace_bytes, bounds.workspace_inodes) == (8 << 20, 64)
    assert (bounds.out_bytes, bounds.out_inodes) == (2 << 20, 16)


@pytest.mark.parametrize(
    ("volume", "change"),
    [
        (
            "workspace",
            {"o": "size=99999m,nr_inodes=500000,uid=65532,gid=65532,mode=0700,nosuid,nodev"},
        ),
        ("workspace", {"type": "none", "device": "/host"}),
        ("out", {"o": "size=1024m,uid=65532,gid=65532,mode=0700,nosuid,nodev,noexec"}),
    ],
)
def test_a_volume_that_does_not_carry_the_profiles_cap_is_refused_before_anything_starts(
    fake_docker: FakeDocker, tmp_path: Path, volume: str, change: dict[str, str]
) -> None:
    spec = make_spec(tmp_path)
    names = store_names_for(spec.container_name)
    name = names.workspace_volume if volume == "workspace" else names.out_volume

    def tamper(_args: tuple[str, ...]) -> None:
        fake_docker.volumes[name]["Options"].update(change)

    fake_docker.hooks["volume inspect"] = tamper

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "volume is not a tmpfs capped at the profile's size and inode count" in stderr
    assert "start" not in fake_docker.verbs
    assert _outcome(spec).state is OutcomeState.REFUSED
    assert fake_docker.containers == {} and fake_docker.volumes == {}


@pytest.mark.parametrize(
    ("volume", "tampered"),
    [
        ("workspace", "size=4096m,nr_inodes=500000,uid=65532,gid=65532,mode=0700"),
        ("out", "size=1024m,nr_inodes=100000,uid=65532,gid=65532,mode=0700,nosuid,nodev"),
    ],
)
def test_a_volume_without_its_mount_flags_is_refused_before_anything_starts(
    fake_docker: FakeDocker, tmp_path: Path, volume: str, tampered: str
) -> None:
    spec = make_spec(tmp_path)
    names = store_names_for(spec.container_name)
    name = names.workspace_volume if volume == "workspace" else names.out_volume

    def tamper(_args: tuple[str, ...]) -> None:
        fake_docker.volumes[name]["Options"]["o"] = tampered

    fake_docker.hooks["volume inspect"] = tamper

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "volume is not mounted with exactly the flags" in stderr
    assert "start" not in fake_docker.verbs
    assert _outcome(spec).state is OutcomeState.REFUSED


def test_a_worker_the_kernel_kills_for_memory_is_an_ordinary_failed_exit(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    """Pages a worker writes into a tmpfs volume are charged to its memory limit, so filling a
    volume past that limit ends in the kernel's OOM kill (137), not in a launcher decision: the
    outcome is an exit like any other, with nothing held for the operator."""
    spec = make_spec(tmp_path)
    fake_docker.exit_code = 137

    code, _ = _run(spec, fake_docker)

    assert code == 137
    outcome = _outcome(spec)
    assert outcome.state is OutcomeState.EXITED and outcome.exit_code == 137
    assert require_exited(outcome, what="worker") == 137


def test_disk_bounds_are_omitted_from_the_profile_state_at_their_defaults() -> None:
    default = RunnerProfile()
    state = default.to_state()["resources"]
    assert isinstance(state, dict)
    assert not {"workspace_mib", "out_mib", "workspace_inodes", "out_inodes"} & set(state)
    assert RunnerProfile.from_state(default.to_state()).digest == default.digest

    tightened = RunnerProfile(resources=ResourceLimits(workspace_mib=8, out_inodes=7))
    tightened_state = tightened.to_state()["resources"]
    assert isinstance(tightened_state, dict)
    assert tightened_state["workspace_mib"] == 8 and tightened_state["out_inodes"] == 7
    assert "out_mib" not in tightened_state
    assert tightened.digest != default.digest
    assert RunnerProfile.from_state(tightened.to_state()) == tightened


# --- private roots ------------------------------------------------------


def test_a_directory_holding_the_trees_that_others_can_enter_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    tmp_path.chmod(0o750)

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "must be mode 0700" in stderr
    assert fake_docker.calls == []


def test_a_directory_holding_the_trees_that_the_operator_does_not_own_is_refused(
    fake_docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = make_spec(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(tmp_path).st_uid + 1)

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "is not owned by the operator" in stderr
    assert fake_docker.calls == []


def test_a_result_directory_outside_the_directory_holding_the_workspace_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    nested = tmp_path / "nested"
    nested.mkdir(mode=0o700)
    spec = make_spec(nested)
    spec = dataclasses.replace(spec, out_path=str(elsewhere))

    code, stderr = _run(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "must live under the directory that holds the workspace" in stderr
    assert fake_docker.calls == []


# --- orphans ----------------------------------------------------------------------------


def test_a_launch_removes_the_orphan_of_a_launcher_that_is_gone_but_never_a_live_one(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    from enginery.engine.supervisor import probe_process

    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        dead_identity = probe_process(dead.pid)
    finally:
        dead.kill()
        dead.wait()
    live_identity = probe_process(os.getpid())
    assert dead_identity is not None and live_identity is not None
    orphan_root, live_root, own_root = (tmp_path / name for name in ("orphan", "live", "own"))
    for root in (orphan_root, live_root, own_root):
        root.mkdir(mode=0o700)
    orphan = make_spec(orphan_root, operation_id="op-orphan")
    live = make_spec(live_root, operation_id="op-live")
    for spec, identity in ((orphan, dead_identity), (live, live_identity)):
        completed = fake_docker.engine().run_cli(
            create_arguments(spec, INNER, nonce="n" * 32, launcher=identity)
        )
        assert completed.returncode == 0

    code, stderr = _run(make_spec(own_root, operation_id="op-own"), fake_docker)

    assert code == 0
    assert (
        f"removed an orphaned container whose launcher is gone: {orphan.container_name}" in stderr
    )
    assert orphan.container_name not in fake_docker.containers
    assert live.container_name in fake_docker.containers


def test_a_launch_labels_its_container_with_its_own_identity(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    from enginery.adapters.oci.engine import LAUNCHER_LABEL
    from enginery.adapters.oci.launch import launcher_label
    from enginery.engine.supervisor import probe_process

    spec = make_spec(tmp_path)
    (tmp_path / "x").mkdir()
    seen: list[str] = []
    fake_docker.hooks["start"] = lambda args: seen.append(
        fake_docker.containers[args[1]].inspect["Config"]["Labels"][LAUNCHER_LABEL]
    )

    _run(spec, fake_docker)

    own = probe_process(os.getpid())
    assert own is not None
    assert seen == [launcher_label(own)] * 2  # the store container and the worker
    assert reconcile_runner_containers(fake_docker.engine()).removed == ()


# --- a real engine ----------------------------------------------------------------------

_STANDIN_IDENTITY = "sha256:" + "5" * 64


def _standin_verifier(_engine: OciEngine, image_id: str) -> ImageIdentity:
    """TEST-ONLY: the stand-in image carries no recipe labels, so it cannot pass the real
    verifier; the launcher is given an identity for it instead."""
    return ImageIdentity(image_id=image_id, recipe_digest=_STANDIN_IDENTITY)


def _real_spec(
    root: Path, operation: str, image: str, profile: RunnerProfile, **overrides: Any
) -> LaunchSpec:
    overrides.setdefault("permissions", PERMISSIONS_WITHOUT_ARGUMENTS)
    return make_spec(root, operation_id=operation, image=image, profile=profile, **overrides)


def _real_run(
    spec: LaunchSpec, engine: OciEngine, command: tuple[str, ...], **options: Any
) -> tuple[int, str]:
    stream = io.StringIO()
    code = run_launcher(
        spec,
        command,
        engine=engine,
        verify=_standin_verifier,
        stderr=stream,
        **options,
    )
    return code, stream.getvalue()


_PLANT_RESULT_LOOP = """
import os, subprocess, sys
loop = '''
import os, sys, time
kind, target, result = sys.argv[1:4]
n = 0
while True:
    n += 1
    tmp = result + '.plant%d' % n
    if kind == 'symlink':
        os.symlink(target, tmp)
    else:
        os.mkfifo(tmp)
    os.replace(tmp, result)
    time.sleep(0.001)
'''
subprocess.Popen(
    [sys.executable, '-c', loop, sys.argv[1], sys.argv[2], '/out/result.json'],
    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
"""


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_real_a_worker_that_swaps_its_result_for_a_link_or_fifo_cannot_make_the_host_follow_it(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
    kind: str,
) -> None:
    """A detached process keeps replacing /out/result.json after the wrapper wrote it, so the
    file the host finds is a symlink to a host file (or a FIFO), not the wrapper's result."""
    secret = real_root / "host-secret.json"
    secret.write_text(json.dumps({"output": "HOST SECRET", "nonce": "x"}), encoding="utf-8")
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)

    code, stderr = _real_run(
        spec, real_engine, ("python3", "-c", _PLANT_RESULT_LOOP, kind, str(secret))
    )

    assert code == 0, stderr
    # The planted link or FIFO stays in the volume: the host never receives it, so there is
    # nothing for it to follow, and no host file was read or replaced.
    result = real_root / "out" / "result.json"
    assert json.loads(secret.read_text("utf-8"))["output"] == "HOST SECRET"
    if os.path.lexists(result):
        # The copy-out raced ahead of the planting loop and found the wrapper's own regular
        # result. That is the legitimate outcome of the same race: what the host holds is a
        # plain file, never the planted link or FIFO, and never the host secret.
        assert stat.S_ISREG(result.lstat().st_mode)
        assert "HOST SECRET" not in read_bounded_text(result, limit=1 << 20, what="result")
    else:
        with pytest.raises(WorkerFailureError, match="missing or unreadable"):
            read_bounded_text(result, limit=1 << 20, what="worker result")
    # The launcher's own outcome is still trustworthy: the container exited zero.
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    assert require_exited(outcome, what="worker") == 0


@pytest.mark.parametrize("exit_code", [124, 125])
def test_real_a_command_that_exits_with_a_launcher_status_is_still_just_an_exit(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
    exit_code: int,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)

    code, _ = _real_run(spec, real_engine, ("python3", "-c", f"import sys; sys.exit({exit_code})"))

    assert code == exit_code
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    assert outcome.state is OutcomeState.EXITED
    assert require_exited(outcome, what="worker") == exit_code
    result = json.loads(
        bind_result_to_attempt(
            read_bounded_text(real_root / "out" / "result.json", limit=1 << 20, what="result"),
            nonce=outcome.nonce,
            what="result",
        )
    )
    assert result["terminal_status"] == "failed"


def test_real_a_stale_result_is_gone_when_the_wrapper_dies_before_writing_a_new_one(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    first, _ = _real_run(spec, real_engine, ("python3", "-c", "print('first attempt')"))
    assert first == 0
    result = real_root / "out" / "result.json"
    old_text = result.read_text("utf-8")
    old_nonce = read_launch_outcome(
        spec.receipt_path, operation_id=spec.operation_id, what="worker"
    ).nonce
    assert old_nonce in old_text
    # The second attempt kills its own wrapper (the command's parent), so no result is written.
    code, _ = _real_run(
        spec,
        real_engine,
        ("python3", "-c", "import os, signal; os.kill(os.getppid(), signal.SIGKILL)"),
    )

    assert code != 0
    assert not os.path.lexists(result), "the first attempt's result was left to be replayed"
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    assert outcome.nonce != old_nonce
    with pytest.raises(WorkerFailureError):
        read_bounded_text(result, limit=1 << 20, what="worker result")
    # And replaying the first result into the second attempt does not bind.
    with pytest.raises(WorkerFailureError, match="not bound to this attempt"):
        bind_result_to_attempt(old_text, nonce=outcome.nonce, what="worker result")


def test_real_the_container_log_is_bounded_whatever_a_worker_prints_to_it(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    """A process in the container can write straight to the container's stdout (pid 1's fd),
    which is the engine's log, not the wrapper's captured output."""
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    flood = (
        "chunk = ('L' * 1023 + '\\n').encode()\n"
        "with open('/proc/1/fd/1', 'wb', buffering=0) as log:\n"
        "    for _ in range(24 * 1024):\n"
        "        log.write(chunk)\n"
    )
    created = real_engine.run_cli(create_arguments(spec, ("python3", "-c", flood), nonce="n" * 32))
    assert created.returncode == 0, created.stderr
    try:
        real_engine.start(spec.container_name)
        real_engine.wait(spec.container_name)
        logs = real_engine.run_cli(("logs", spec.container_name), timeout=120)
        written = 24 * 1024 * 1024
        logged = len(logs.stdout.encode()) + len(logs.stderr.encode())
        # The wrapper captures the flood too (it is the command's own stdout) and ends up
        # in the result; the engine's log, which is what is bounded, is far smaller.
        assert logged < 4 * 1024 * 1024, f"container log held {logged} of {written} bytes"
    finally:
        real_engine.remove(spec.container_name)


_FILL = """
import errno, os, sys
role, size = sys.argv[1], int(sys.argv[2])
directory = '/workspace' if role == 'workspace' else '/out'
code = 'none'
paths = []
try:
    for index in range(size >> 20):
        path = '%s/chunk%d' % (directory, index)
        paths.append(path)
        with open(path, 'wb') as handle:
            handle.write(b'x' * (1 << 20))
except OSError as error:
    code = errno.errorcode[error.errno]
if role == 'out':
    for path in paths:  # leave room for the wrapper's own result file
        if os.path.exists(path):
            os.unlink(path)
print('fill-result', code)
"""

_FALLOCATE_BURST = """
import errno, os, subprocess, sys
children = []
for index in range(8):
    children.append(subprocess.Popen(
        ['fallocate', '-l', '64M', '/workspace/burst%d' % index],
        stderr=subprocess.PIPE,
    ))
failures = sum(1 for child in children if child.wait() != 0)
total = sum(os.stat('/workspace/' + name).st_size for name in os.listdir('/workspace'))
print('burst-failures', failures, 'allocated', total)
"""

_MANY_FILES = """
import errno, os
code = 'none'
try:
    for index in range(10_000):
        open('/workspace/f%d' % index, 'w').close()
except OSError as error:
    code = errno.errorcode[error.errno]
print('files-result', code, len(os.listdir('/workspace')))
"""


def _small_caps(profile: RunnerProfile, **limits: int) -> RunnerProfile:
    return dataclasses.replace(profile, resources=dataclasses.replace(profile.resources, **limits))


def _output(real_root: Path, spec: LaunchSpec) -> str:
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    text = bind_result_to_attempt(
        read_bounded_text(real_root / "out" / spec.result_name, limit=1 << 20, what="result"),
        nonce=outcome.nonce,
        what="result",
    )
    return str(json.loads(text)["output"])


@pytest.mark.parametrize("role", ["workspace", "out"])
def test_real_a_write_past_the_volume_cap_fails_with_enospc_and_nothing_is_killed(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
    role: str,
) -> None:
    profile = _small_caps(real_profile, workspace_mib=8, out_mib=8)
    spec = _real_spec(real_root, real_operation(), standin_image, profile)

    code, _ = _real_run(spec, real_engine, ("python3", "-c", _FILL, role, str(64 << 20)))

    assert code == 0
    assert "fill-result ENOSPC" in _output(real_root, spec)
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    assert outcome.state is OutcomeState.EXITED
    receipt = RunnerReceipt.from_state(json.loads(Path(spec.receipt_path).read_text("utf-8")))
    assert receipt.bounds is not None
    assert receipt.bounds.disk_mechanism == DISK_BOUND_KERNEL_VOLUME
    # The worker's oversize attempt never reached the host: at most the capped tree came back.
    returned = sum(
        entry.stat().st_size for entry in (real_root / "workspace").rglob("*") if entry.is_file()
    )
    assert returned <= 8 << 20


def test_real_a_parallel_fallocate_burst_is_capped_by_the_kernel(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    """Eight processes each ask for 64 MiB of a 16 MiB workspace at once: the kernel refuses
    with ENOSPC; allocation never exceeds the cap."""
    profile = _small_caps(real_profile, workspace_mib=16)
    spec = _real_spec(real_root, real_operation(), standin_image, profile)

    code, _ = _real_run(spec, real_engine, ("python3", "-c", _FALLOCATE_BURST))

    assert code == 0
    output = _output(real_root, spec)
    failures, allocated = [int(token) for token in output.split() if token.isdigit()][:2]
    assert failures == 8
    assert allocated <= 16 << 20


def test_real_the_inode_cap_stops_a_worker_creating_too_many_entries(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    profile = _small_caps(real_profile, workspace_inodes=200)
    spec = _real_spec(real_root, real_operation(), standin_image, profile)

    code, _ = _real_run(spec, real_engine, ("python3", "-c", _MANY_FILES))

    assert code == 0
    output = _output(real_root, spec)
    assert "files-result ENOSPC" in output
    assert int(output.split("files-result ENOSPC")[1].split()[0]) <= 200


_MOUNT_FLAGS = """
import os, subprocess
flags = {}
for line in open('/proc/self/mountinfo'):
    fields = line.split()
    if fields[4] in ('/workspace', '/out'):
        flags[fields[4]] = sorted(fields[5].split(','))
for path in ('/workspace', '/out'):
    script = path + '/probe.sh'
    try:
        with open(script, 'w') as handle:
            handle.write('#!/bin/sh\\nexit 0\\n')
        os.chmod(script, 0o4755)
        code = subprocess.run([script]).returncode
    except OSError as error:
        code = 'E' + str(error.errno)
    print('flags', path, ','.join(flags[path]), 'exec', code)
"""


def test_real_the_workspace_is_nosuid_and_nodev_and_the_result_volume_is_also_noexec(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    """The kernel mounts report the flags; a setuid script can run from the workspace (which
    the project's own commands need) but never from the noexec result volume."""
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)

    code, _ = _real_run(spec, real_engine, ("python3", "-c", _MOUNT_FLAGS))

    assert code == 0
    lines = {line.split()[1]: line for line in _output(real_root, spec).splitlines()}
    workspace, out = lines["/workspace"].split(), lines["/out"].split()
    assert {"nosuid", "nodev"} <= set(workspace[2].split(",")) and "noexec" not in workspace[2]
    assert workspace[-1] == "0"
    assert {"nosuid", "nodev", "noexec"} <= set(out[2].split(","))
    assert out[-1] == "E13"  # EACCES: execution from the result volume is refused


def test_real_the_workspace_comes_back_replaced_and_deletable_by_the_host(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    (real_root / "workspace" / "host.txt").write_text("host", encoding="utf-8")
    script = (
        "import os\n"
        "open('/workspace/host.txt', 'a').write('+worker')\n"
        "os.mkdir('/workspace/nested')\n"
        "open('/workspace/nested/inner.txt', 'w').write('x')\n"
        "os.chmod('/workspace/nested/inner.txt', 0)\n"
        "os.chmod('/workspace/nested', 0o500)\n"
    )

    code, _ = _real_run(spec, real_engine, ("python3", "-c", script))

    workspace = real_root / "workspace"
    assert code == 0
    assert (workspace / "host.txt").read_text() == "host+worker"
    assert (workspace / "nested" / "inner.txt").read_text() == "x"
    shutil.rmtree(workspace / "nested")  # whatever modes the worker chose, the host can delete


_GROUP_DRIVER = """
import json, sys
from enginery.adapters.oci.launcher import main
from enginery.domain.runner import ImageIdentity

def verify(engine, image_id):
    return ImageIdentity(image_id=image_id, recipe_digest="sha256:" + "5" * 64)

spec_json, *inner = sys.argv[1:]
raise SystemExit(main(["--spec", spec_json, "--", *inner], verify=verify))
"""


def _start_driver(spec: LaunchSpec, command: tuple[str, ...]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _GROUP_DRIVER, json.dumps(spec.to_state()), *command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # its own process group, like a supervised launcher
    )


def _await_running(engine: OciEngine, spec: LaunchSpec, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and process.poll() is None:
        try:
            state = engine.container_inspect(spec.container_name)["State"]
        except Exception:
            state = {}
        if state.get("Running") is True:
            return
        time.sleep(0.3)
    pytest.fail("the worker container never started")


def _await_outcome(spec: LaunchSpec, state: OutcomeState) -> None:
    """The container can be running a moment before the launcher records that it is."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if _outcome(spec).state is state:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    pytest.fail(f"the launcher never recorded {state.value}")


def test_real_a_stop_signal_to_the_whole_launcher_process_group_still_removes_the_container(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    """The supervisor signals the launcher's whole group, which also reaches the ``docker wait``
    child the launcher is blocked on: the launcher must still kill and remove the container."""
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    process = _start_driver(spec, ("python3", "-c", "import time; time.sleep(120)"))
    try:
        _await_running(real_engine, spec, process)
        os.killpg(process.pid, signal.SIGTERM)
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()

    assert process.returncode in (143, EXIT_FAULT), stderr
    assert not real_engine.container_exists(spec.container_name)
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    assert outcome.state in (OutcomeState.INTERRUPTED, OutcomeState.FAULTED)
    with pytest.raises(WorkerFailureError):
        require_exited(outcome, what="worker")


def test_real_a_launcher_group_that_is_killed_outright_leaves_a_container_reconcile_removes(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(real_root, real_operation(), standin_image, real_profile)
    process = _start_driver(spec, ("python3", "-c", "import time; time.sleep(120)"))
    try:
        _await_running(real_engine, spec, process)
        _await_outcome(spec, OutcomeState.RUNNING)
        os.killpg(process.pid, signal.SIGKILL)  # no chance to clean up
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    assert real_engine.container_exists(spec.container_name), "nothing was orphaned"
    # While the launcher's process is gone, the lost launcher's outcome is not a success.
    outcome = read_launch_outcome(spec.receipt_path, operation_id=spec.operation_id, what="worker")
    assert outcome.state is OutcomeState.RUNNING
    with pytest.raises(WorkerFailureError, match="did not finish"):
        require_exited(outcome, what="worker")

    report = reconcile_runner_containers(real_engine, keep=frozenset())

    assert spec.container_name in report.removed
    assert not real_engine.container_exists(spec.container_name)
