"""Reconcile removes runner containers whose launcher process is gone, and only those."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from enginery.adapters.oci.engine import LAUNCHER_LABEL
from enginery.adapters.oci.launch import create_arguments, launcher_label
from enginery.adapters.oci.launcher import main, reconcile_main
from enginery.adapters.oci.reconcile import reconcile_runner_containers
from enginery.engine.supervisor import (
    HostIdentity,
    ProcessIdentity,
    probe_host,
    probe_process,
)

from .support import INNER, NONCE, FakeDocker, make_spec


@pytest.fixture
def dead_launcher() -> Iterator[ProcessIdentity]:
    """The identity of a real process that has since exited (its pid may even be reused)."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = probe_process(process.pid)
        assert identity is not None
    finally:
        process.kill()
        process.wait()
    yield identity


@pytest.fixture
def live_launcher() -> ProcessIdentity:
    identity = probe_process(os.getpid())
    assert identity is not None
    return identity


def _create(
    fake: FakeDocker,
    root: Path,
    operation_id: str,
    launcher: ProcessIdentity | None,
) -> str:
    """A worker container for ``operation_id`` as a launcher with identity ``launcher`` made it."""
    root.mkdir(parents=True, exist_ok=True)
    spec = make_spec(root, operation_id=operation_id)
    completed = fake.engine().run_cli(create_arguments(spec, INNER, nonce=NONCE, launcher=launcher))
    assert completed.returncode == 0, completed.stderr
    return spec.container_name


def test_a_container_whose_launcher_is_gone_is_removed(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-orphan", dead_launcher)

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.removed == (name,)
    assert fake_docker.containers == {}
    assert report.kept == () and report.failed == ()


def test_a_container_whose_launcher_is_alive_is_never_touched(
    fake_docker: FakeDocker, tmp_path: Path, live_launcher: ProcessIdentity
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-live", live_launcher)

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.kept == (name,)
    assert report.removed == ()
    assert name in fake_docker.containers
    assert "rm" not in fake_docker.verbs


def test_a_pid_reused_by_another_process_does_not_keep_the_container_alive(
    fake_docker: FakeDocker, tmp_path: Path, live_launcher: ProcessIdentity
) -> None:
    # Same pid as a live process but a different start identity: that is not the launcher.
    reused = ProcessIdentity(
        live_launcher.pid,
        live_launcher.process_group_id,
        live_launcher.start_identity + " (an earlier process)",
        live_launcher.host,
    )
    name = _create(fake_docker, tmp_path / "a", "op-reused", reused)

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.removed == (name,)


def test_a_container_recorded_for_an_earlier_boot_is_removed_whatever_its_pid_is_now(
    fake_docker: FakeDocker, tmp_path: Path, live_launcher: ProcessIdentity
) -> None:
    here = probe_host()
    earlier = ProcessIdentity(
        live_launcher.pid,
        live_launcher.process_group_id,
        live_launcher.start_identity,
        HostIdentity(here.machine_id, here.boot_id + "-earlier-boot"),
    )
    name = _create(fake_docker, tmp_path / "a", "op-boot", earlier)

    assert reconcile_runner_containers(fake_docker.engine()).removed == (name,)


def test_a_container_of_another_machine_cannot_be_judged_and_is_left_alone(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    other = ProcessIdentity(
        dead_launcher.pid,
        dead_launcher.process_group_id,
        dead_launcher.start_identity,
        HostIdentity("another-machine", "another-boot"),
    )
    name = _create(fake_docker, tmp_path / "a", "op-elsewhere", other)

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.unattributed == (name,)
    assert name in fake_docker.containers


def test_a_runner_container_without_a_launcher_label_is_reported_not_removed(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-legacy", None)

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.unattributed == (name,)
    assert name in fake_docker.containers


@pytest.mark.parametrize(
    "label",
    [
        "not json",
        "[]",
        '{"pid": "x"}',
        '{"pid": 1, "pgid": 1, "start": "", "machine": "m", "boot": "b"}',
    ],
)
def test_a_malformed_launcher_label_is_never_a_reason_to_remove(
    fake_docker: FakeDocker, tmp_path: Path, live_launcher: ProcessIdentity, label: str
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-bad-label", live_launcher)
    fake_docker.containers[name].inspect["Config"]["Labels"][LAUNCHER_LABEL] = label

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.unattributed == (name,)
    assert name in fake_docker.containers


def test_the_callers_own_container_is_kept_even_if_its_launcher_looks_gone(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-mine", dead_launcher)

    report = reconcile_runner_containers(fake_docker.engine(), keep=frozenset({name}))

    assert report.kept == (name,)
    assert name in fake_docker.containers


def test_containers_that_are_not_runner_containers_are_never_listed_or_removed(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    orphan = _create(fake_docker, tmp_path / "a", "op-orphan", dead_launcher)
    # A foreign container: no runner label.
    (tmp_path / "b").mkdir()
    spec = make_spec(tmp_path / "b", operation_id="foreign")
    fake_docker.create_worker(spec)
    fake_docker.containers[spec.container_name].inspect["Config"]["Labels"] = {}

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.removed == (orphan,)
    assert spec.container_name in fake_docker.containers


def test_a_failing_removal_is_reported_and_the_pass_continues(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    first = _create(fake_docker, tmp_path / "a", "op-1", dead_launcher)
    second = _create(fake_docker, tmp_path / "b", "op-2", dead_launcher)
    fake_docker.failures["rm"] = (1, "Error response from daemon: device busy")

    report = reconcile_runner_containers(fake_docker.engine())

    assert report.removed == ()
    assert {name for name, _ in report.failed} == {first, second}


def test_the_launcher_labels_its_own_container_with_its_identity(
    fake_docker: FakeDocker, tmp_path: Path, live_launcher: ProcessIdentity
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-label", live_launcher)

    labels = fake_docker.containers[name].inspect["Config"]["Labels"]

    assert labels[LAUNCHER_LABEL] == launcher_label(live_launcher)
    assert str(live_launcher.pid) in labels[LAUNCHER_LABEL]


def test_the_command_line_reconcile_removes_orphans_and_reports_each(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    name = _create(fake_docker, tmp_path / "a", "op-cli", dead_launcher)
    stream = io.StringIO()

    code = reconcile_main(["--reconcile"], engine=fake_docker.engine(), stdout=stream)

    assert code == 0
    assert f"removed orphaned runner container {name}" in stream.getvalue()
    assert fake_docker.containers == {}


def test_the_command_line_reconcile_exits_non_zero_when_a_container_cannot_be_removed(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    _create(fake_docker, tmp_path / "a", "op-cli", dead_launcher)
    fake_docker.failures["rm"] = (1, "Error response from daemon: device busy")

    assert reconcile_main(["--reconcile"], engine=fake_docker.engine(), stdout=io.StringIO()) == 125


def test_main_dispatches_reconcile_and_rejects_unknown_flags(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    _create(fake_docker, tmp_path / "a", "op-main", dead_launcher)

    assert main(["--reconcile"], engine=fake_docker.engine()) == 0
    assert fake_docker.containers == {}
    assert main(["--reconcile", "--force"], engine=fake_docker.engine()) == 125


def test_reconcile_through_the_docker_context_flag(
    fake_docker: FakeDocker, tmp_path: Path, dead_launcher: ProcessIdentity
) -> None:
    _create(fake_docker, tmp_path / "a", "op-ctx", dead_launcher)
    engine = fake_docker.engine(context="lima-rootless")

    assert reconcile_main(["--reconcile"], engine=engine, stdout=io.StringIO()) == 0
    assert fake_docker.context == "lima-rootless"


def test_a_launcher_pid_that_is_alive_in_a_group_is_kept(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    """A real launcher-like child in its own session: alive means kept, then gone means removed."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        identity = probe_process(child.pid)
        assert identity is not None
        name = _create(fake_docker, tmp_path / "a", "op-group", identity)

        assert reconcile_runner_containers(fake_docker.engine()).kept == (name,)
        child.kill()
        child.wait()
        deadline = time.monotonic() + 5
        while probe_process(child.pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert reconcile_runner_containers(fake_docker.engine()).removed == (name,)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
