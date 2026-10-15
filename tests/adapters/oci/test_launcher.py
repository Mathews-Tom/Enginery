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
from enginery.adapters.oci.launch import engine_memory_floor_mib, store_names_for
from enginery.adapters.oci.launcher import (
    EXIT_DEADLINE,
    EXIT_FAULT,
    StopSignal,
    admit_paths,
    main,
    run_launcher,
    write_receipt,
)
from enginery.domain.errors import MissingPrerequisiteError
from enginery.domain.runner import (
    ImageIdentity,
    LaunchOutcome,
    LaunchSpec,
    OutcomeState,
    ResourceLimits,
    RunnerProfile,
    RunnerReceipt,
    TrustMode,
    outcome_path_for,
    profile_violations,
)
from enginery.engine.supervisor import probe_host

from .support import (
    HOST,
    IMAGE_ID,
    INNER,
    PERMISSIONS,
    PERMISSIONS_WITHOUT_ARGUMENTS,
    FakeDocker,
    FakeNetwork,
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
        "info",
        "container inspect",
        "container inspect",
        "volume rm",
        "volume rm",
        "ps",
        "volume ls",
        "network ls",
        "create",
        "volume inspect",
        "volume inspect",
        "container inspect",
        "image inspect",
        "start",
        "cp",
        "create",
        "container inspect",
        "image inspect",
        "start",
        "wait",
        "cp",
        "cp",
        "rm",
        "rm",
        "volume rm",
        "volume rm",
    ]
    assert fake_docker.containers == {}
    # No relay was asked for: the worker ran with no network, and no network was ever created.
    assert fake_docker.networks == {}
    assert fake_docker.calls_of("network create") == []
    assert fake_docker.calls_of("network connect") == []


def _attempt_launch(
    tmp_path: Path, fake: FakeDocker, *, operation_id: str = "op-1", **kwargs: Any
) -> tuple[LaunchSpec, int, str]:
    spec = make_spec(tmp_path, operation_id=operation_id, relay_container="enginery-relay")
    code, stderr = _launch(spec, fake, **kwargs)
    return spec, code, stderr


def _relay_networks(fake: FakeDocker) -> list[str]:
    document = fake.containers["enginery-relay"].inspect
    return sorted(document["NetworkSettings"]["Networks"])


def _started(fake: FakeDocker) -> list[str]:
    """Names of the containers the launcher started, in order (the store one first, to seed)."""
    return [call[1] for call in fake.calls_of("start")]


def test_a_worker_with_model_access_gets_its_own_network_with_only_the_relay_attached(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    spec = make_spec(tmp_path, relay_container="enginery-relay")
    network = spec.attempt_network
    seen: dict[str, Any] = {}

    def at_start(args: tuple[str, ...]) -> None:
        if args[-1] != spec.container_name:  # the store container starts first, for seeding
            return
        seen["members"] = fake_docker.network_members(network)
        seen["labels"] = dict(fake_docker.networks[network].labels)
        seen["internal"] = fake_docker.networks[network].internal
        seen["relay_networks"] = _relay_networks(fake_docker)
        endpoints = fake_docker.containers["enginery-relay"].inspect["NetworkSettings"]
        seen["relay_aliases"] = endpoints["Networks"][network]["Aliases"]
        seen["receipt_present"] = os.path.exists(spec.receipt_path)

    fake_docker.hooks["start"] = at_start

    code, stderr = _launch(spec, fake_docker)

    assert code == 0, stderr
    assert network.startswith(f"{spec.profile.network}-")
    # When the worker is started: its own internal network, labelled for this operation and
    # this launcher, with the relay (reachable as ``enginery-relay``) as its only member.
    assert seen["internal"] is True
    assert seen["labels"]["dev.enginery.attempt-network"] == "op-1"
    assert "dev.enginery.launcher" in seen["labels"]
    assert seen["members"] == ["enginery-relay"]
    assert seen["relay_networks"] == sorted(["enginery-runner-egress", network])
    assert seen["relay_aliases"][0] == "enginery-relay"
    # The receipt, written before the start, records exactly that network.
    assert seen["receipt_present"] is True
    receipt = _receipt(spec)
    assert receipt.effective.network_mode == network
    assert receipt.effective.networks == (network,)
    assert profile_violations(spec.profile, receipt.effective, network=network) == ()
    # At the end the relay left the network, the network is gone and the relay is untouched.
    assert fake_docker.networks == {}
    assert _relay_networks(fake_docker) == ["enginery-runner-egress"]
    assert fake_docker.containers["enginery-relay"].state == "running"


def test_a_worker_with_model_access_is_launched_in_the_order_network_connect_start_release(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()

    _, code, stderr = _attempt_launch(tmp_path, fake_docker)

    assert code == 0, stderr
    assert fake_docker.verbs == [
        "info",
        "info",
        "container inspect",
        "container inspect",
        "volume rm",
        "volume rm",
        "ps",
        "volume ls",
        "network ls",
        "network inspect",
        "network create",
        "network inspect",
        "create",
        "volume inspect",
        "volume inspect",
        "container inspect",
        "image inspect",
        "start",
        "cp",
        "create",
        "container inspect",
        "image inspect",
        "container inspect",
        "network connect",
        "network inspect",
        "start",
        "wait",
        "cp",
        "cp",
        "rm",
        "network disconnect",
        "network inspect",
        "network rm",
        "rm",
        "volume rm",
        "volume rm",
    ]
    (connect,) = fake_docker.calls_of("network connect")
    assert connect[:3] == ("network", "connect", "--alias")
    assert connect[3] == "enginery-relay"
    assert connect[-1] == "enginery-relay"


def test_a_worker_without_model_access_never_meets_the_relay(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    spec = make_spec(tmp_path)

    code, stderr = _launch(spec, fake_docker)

    assert code == 0, stderr
    assert fake_docker.calls_of("network create") == []
    assert fake_docker.calls_of("network connect") == []
    store, worker = fake_docker.calls_of("create")
    assert store[store.index("--network") + 1] == "none"
    assert worker[worker.index("--network") + 1] == "none"
    assert _relay_networks(fake_docker) == ["enginery-runner-egress"]


def test_two_concurrent_attempts_get_two_networks_and_the_relay_is_on_both(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    fake_docker.hold_running = True
    roots = (tmp_path / "a", tmp_path / "b")
    for root in roots:
        root.mkdir(mode=0o700)
    first = make_spec(roots[0], operation_id="op-a", relay_container="enginery-relay")
    second = make_spec(roots[1], operation_id="op-b", relay_container="enginery-relay")
    stops = (StopSignal(), StopSignal())
    results: dict[str, tuple[int, str]] = {}

    def run(spec: LaunchSpec, stop: StopSignal) -> None:
        results[spec.operation_id] = _launch(spec, fake_docker, stop=stop)

    threads = [
        threading.Thread(target=run, args=(spec, stop))
        for spec, stop in zip((first, second), stops, strict=True)
    ]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(
            spec.container_name in fake_docker.containers
            and fake_docker.containers[spec.container_name].state == "running"
            for spec in (first, second)
        ):
            time.sleep(0.01)
        assert first.attempt_network != second.attempt_network
        assert set(fake_docker.networks) == {first.attempt_network, second.attempt_network}
        # Each network holds exactly its own worker and the relay, never the other worker.
        assert fake_docker.network_members(first.attempt_network) == sorted(
            [first.container_name, "enginery-relay"]
        )
        assert fake_docker.network_members(second.attempt_network) == sorted(
            [second.container_name, "enginery-relay"]
        )
    finally:
        for stop in stops:
            stop.request(15)
        for thread in threads:
            thread.join(timeout=15)

    assert {code for code, _ in results.values()} == {128 + 15}
    assert fake_docker.networks == {}
    assert _relay_networks(fake_docker) == ["enginery-runner-egress"]


@pytest.mark.parametrize(
    ("relay", "message"),
    [
        (None, "does not exist"),
        ({"running": False}, "not running"),
        ({"restarting": True}, "not running"),
    ],
)
def test_a_relay_that_cannot_be_attached_refuses_the_launch_and_cleans_up(
    fake_docker: FakeDocker, tmp_path: Path, relay: dict[str, bool] | None, message: str
) -> None:
    if relay is not None:
        fake_docker.add_relay(**relay)

    spec, code, stderr = _attempt_launch(tmp_path, fake_docker)

    assert code == EXIT_FAULT
    assert "cannot be attached to the attempt's network" in stderr
    assert message in stderr
    assert _started(fake_docker) == [store_names_for(spec.container_name).holder]
    assert fake_docker.containers.keys() <= {"enginery-relay"}
    assert fake_docker.networks == {}
    assert not os.path.lexists(spec.receipt_path)
    outcome = LaunchOutcome.from_state(
        json.loads(Path(outcome_path_for(spec.receipt_path)).read_text("utf-8"))
    )
    assert outcome.state is OutcomeState.REFUSED


def test_a_container_that_is_not_the_relay_is_never_attached(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    fake_docker.containers["enginery-relay"].inspect["Config"]["Labels"] = {}

    _, code, stderr = _attempt_launch(tmp_path, fake_docker)

    assert code == EXIT_FAULT
    assert "exists but is not the relay" in stderr
    assert "network connect" not in fake_docker.verbs
    assert fake_docker.networks == {}


def test_an_attempt_network_shared_with_anything_but_the_relay_is_refused_before_the_start(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    spec = make_spec(tmp_path, relay_container="enginery-relay")

    def intruder(args: tuple[str, ...]) -> None:
        # Another container joins the attempt's network right after the relay was connected.
        members = fake_docker.network_members(spec.attempt_network)
        joined = spec.attempt_network in fake_docker.networks and members == ["enginery-relay"]
        if joined and "intruder" not in fake_docker.containers:
            fake_docker.add_relay("intruder")
            endpoints = fake_docker.containers["intruder"].inspect["NetworkSettings"]
            endpoints["Networks"][spec.attempt_network] = {}

    fake_docker.hooks["network inspect"] = intruder

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "members beyond the relay" in stderr
    assert _started(fake_docker) == [store_names_for(spec.container_name).holder]
    assert fake_docker.networks == {}


def test_a_launch_that_is_stopped_releases_the_relay_and_the_network(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    fake_docker.hold_running = True
    stop = StopSignal()
    _stop_after_start(fake_docker, stop, 15)

    _, code, _ = _attempt_launch(tmp_path, fake_docker, stop=stop)

    assert code == 128 + 15
    assert fake_docker.containers.keys() == {"enginery-relay"}
    assert fake_docker.networks == {}
    assert _relay_networks(fake_docker) == ["enginery-runner-egress"]


def test_a_network_that_cannot_be_removed_is_reported_and_left_for_reconcile(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    fake_docker.failures["network rm"] = (1, "Error response from daemon: boom")

    spec, code, stderr = _attempt_launch(tmp_path, fake_docker)

    assert code == 0  # the worker's own status is untouched
    assert "the attempt network could not be removed" in stderr
    assert spec.attempt_network in fake_docker.networks
    assert fake_docker.network_members(spec.attempt_network) == []  # the relay did detach


def test_the_network_of_an_earlier_attempt_of_the_same_operation_is_replaced(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    spec = make_spec(tmp_path, relay_container="enginery-relay")
    fake_docker.engine().create_attempt_network(spec.attempt_network, operation_id="op-1")
    fake_docker.engine().connect_network(spec.attempt_network, "enginery-relay")

    code, stderr = _launch(spec, fake_docker)

    assert code == 0, stderr
    assert len(fake_docker.calls_of("network create")) == 2
    assert fake_docker.networks == {}


def test_the_launcher_removes_an_orphaned_attempt_network_before_it_starts(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    fake_docker.add_relay()
    here = probe_host()
    dead = {
        "dev.enginery.attempt-network": "op-dead",
        "dev.enginery.launcher": json.dumps(
            {
                "pid": 1,
                "pgid": 1,
                "start": "gone",
                "machine": here.machine_id,
                "boot": "an-earlier-boot",
            }
        ),
    }
    fake_docker.networks["enginery-runner-internal-dead"] = FakeNetwork(internal=True, labels=dead)
    fake_docker.engine().connect_network("enginery-runner-internal-dead", "enginery-relay")

    spec, code, stderr = _attempt_launch(tmp_path, fake_docker)

    assert code == 0, stderr
    assert "removed an orphaned attempt network whose launcher is gone" in stderr
    assert fake_docker.networks == {}
    assert spec.attempt_network != "enginery-runner-internal-dead"


def test_the_receipt_records_what_the_engine_reported_and_is_written_before_the_start(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, env=(("ANTHROPIC_AUTH_TOKEN", "dummy-credential-4f8a91c2"),))
    receipt_present_at_start: list[bool] = []
    fake_docker.hooks["start"] = lambda args: (
        receipt_present_at_start.append(os.path.exists(spec.receipt_path))
        if args[1] == spec.container_name
        else None
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
    assert profile_violations(spec.profile, receipt.effective, network="none") == ()
    assert receipt.effective.network_mode == "none"
    assert receipt.effective.networks == ("none",)
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

    store, worker = fake_docker.calls_of("create")
    assert store[0] == worker[0] == "create"
    assert store[store.index("--name") + 1] == f"{spec.container_name}-store"
    assert worker[worker.index("--name") + 1] == spec.container_name
    assert worker[worker.index("--network") + 1] == "none"
    assert store[store.index("--network") + 1] == "none"
    assert worker[worker.index(spec.image) :][:3] == (
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
        "network ls",
        "container inspect",
        "volume inspect",
        "volume rm",
        "volume ls",
        "ps",
        "create",
        "image inspect",
        "start",
        "cp",
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


def test_the_worker_is_given_a_seeded_workspace_through_the_store_and_never_a_host_path(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir()
    (workspace / "src" / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (workspace / "src" / "tool.sh").chmod(0o700)
    (workspace / "notes.txt").write_text("n", encoding="utf-8")
    (workspace / "link").symlink_to("notes.txt")
    seen: dict[str, str] = {}

    def at_worker_start(args: tuple[str, ...]) -> None:
        if args[1] == spec.container_name:
            volume = fake_docker.workspace_volume(spec)
            seen["tool"] = (volume / "src" / "tool.sh").read_text()
            seen["notes"] = (volume / "notes.txt").read_text()
            seen["link"] = os.readlink(volume / "link")

    fake_docker.hooks["start"] = at_worker_start

    code, _ = _launch(spec, fake_docker)

    assert code == 0
    assert seen == {"tool": "#!/bin/sh\n", "notes": "n", "link": "notes.txt"}
    # No engine command ever names a host path: the host trees reach the worker only as a tar
    # stream through the store container.
    for call in fake_docker.calls:
        assert str(tmp_path) not in " ".join(call)
    worker = fake_docker.calls_of("create")[1]
    assert "-v" not in worker[: worker.index(spec.image)]


def test_the_result_the_bundle_and_the_workspace_come_back_through_the_store(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, bundle_branch="work", bundle_base="a" * 40)
    workspace = tmp_path / "workspace"
    (workspace / "doomed.txt").write_text("host", encoding="utf-8")
    (workspace / "kept.txt").write_text("before", encoding="utf-8")

    def at_worker_start(args: tuple[str, ...]) -> None:
        if args[1] != spec.container_name:
            return
        volume = fake_docker.workspace_volume(spec)
        (volume / "doomed.txt").unlink()
        (volume / "kept.txt").write_text("after", encoding="utf-8")
        (volume / "new").mkdir()
        (volume / "new" / "file.txt").write_text("created by the worker", encoding="utf-8")
        out = fake_docker.out_volume(spec)
        (out / "result.json").write_text('{"ok": true}', encoding="utf-8")
        (out / "branch.bundle").write_bytes(b"bundle bytes")
        (out / "ignored.txt").write_text("not a named result", encoding="utf-8")

    fake_docker.hooks["start"] = at_worker_start

    code, _ = _launch(spec, fake_docker)

    assert code == 0
    assert (tmp_path / "out" / "result.json").read_text() == '{"ok": true}'
    assert (tmp_path / "out" / "branch.bundle").read_bytes() == b"bundle bytes"
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [
        "branch.bundle",
        "result.json",
    ]
    assert sorted(path.name for path in workspace.iterdir()) == ["kept.txt", "new"]
    assert (workspace / "kept.txt").read_text() == "after"
    assert (workspace / "new" / "file.txt").read_text() == "created by the worker"
    assert sorted(path.name for path in tmp_path.iterdir() if path.name.startswith(".")) == []
    state = json.loads(Path(outcome_path_for(spec.receipt_path)).read_text("utf-8"))
    assert state["state"] == "exited" and state["exit_code"] == 0


def test_the_store_container_holds_the_volumes_until_everything_is_copied_out(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    names = store_names_for(spec.container_name)
    removed_while_holding: list[bool] = []

    def at_cp(args: tuple[str, ...]) -> None:
        removed_while_holding.append(names.holder in fake_docker.containers)

    fake_docker.hooks["cp"] = at_cp

    code, _ = _launch(spec, fake_docker)

    assert code == 0
    assert removed_while_holding and all(removed_while_holding)
    assert fake_docker.verbs[-4:] == ["rm", "rm", "volume rm", "volume rm"]
    assert fake_docker.calls_of("rm")[-1][-1] == names.holder
    assert fake_docker.containers == {} and fake_docker.volumes == {}


def test_a_symlink_or_fifo_the_worker_leaves_as_its_result_is_not_copied_to_the_host(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    secret = tmp_path / "host-secret.txt"
    secret.write_text("host secret", encoding="utf-8")

    def at_worker_start(args: tuple[str, ...]) -> None:
        if args[1] == spec.container_name:
            (fake_docker.out_volume(spec) / "result.json").symlink_to(secret)

    fake_docker.hooks["start"] = at_worker_start

    code, stderr = _launch(spec, fake_docker)

    assert code == 0  # the worker exited; the missing result is collection's refusal
    assert not os.path.lexists(tmp_path / "out" / "result.json")
    assert "is not a regular file" in stderr
    assert secret.read_text() == "host secret"

    def fifo_at_worker_start(args: tuple[str, ...]) -> None:
        if args[1] == spec.container_name:
            os.mkfifo(fake_docker.out_volume(spec) / "result.json")

    fake_docker.hooks["start"] = fifo_at_worker_start
    code, _ = _launch(spec, fake_docker)
    assert code == 0
    assert not os.path.lexists(tmp_path / "out" / "result.json")


def test_a_link_or_special_file_in_the_returned_workspace_never_escapes_the_host_tree(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    def at_worker_start(args: tuple[str, ...]) -> None:
        if args[1] != spec.container_name:
            return
        volume = fake_docker.workspace_volume(spec)
        (volume / "hop").symlink_to(outside)
        os.mkfifo(volume / "pipe")
        (volume / "real.txt").write_text("ok", encoding="utf-8")
        os.link(volume / "real.txt", volume / "alias.txt")

    fake_docker.hooks["start"] = at_worker_start

    code, _ = _launch(spec, fake_docker)

    workspace = tmp_path / "workspace"
    assert code == 0
    assert os.readlink(workspace / "hop") == str(outside)  # kept as a link, never followed
    assert not os.path.lexists(workspace / "pipe")
    assert (workspace / "alias.txt").read_text() == "ok"
    assert not (workspace / "alias.txt").samefile(workspace / "real.txt")
    assert list(outside.iterdir()) == []


def test_a_workspace_that_does_not_fit_its_volume_is_refused_before_the_worker_exists(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    profile = RunnerProfile(resources=ResourceLimits(workspace_mib=1))
    spec = make_spec(tmp_path, profile=profile)
    (tmp_path / "workspace" / "big.bin").write_bytes(b"\0" * (2 << 20))

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "cannot be given to the worker" in stderr
    assert len(fake_docker.calls_of("create")) == 1  # only the store container was created
    assert fake_docker.containers == {} and fake_docker.volumes == {}
    assert not os.path.lexists(spec.receipt_path)
    state = json.loads(Path(outcome_path_for(spec.receipt_path)).read_text("utf-8"))
    assert state["state"] == "refused"


def _state_of(spec: LaunchSpec) -> object:
    return json.loads(Path(outcome_path_for(spec.receipt_path)).read_text("utf-8"))["state"]


def test_an_engine_too_small_for_one_worker_is_refused_before_any_container_exists(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    floor = engine_memory_floor_mib(spec.profile)
    fake_docker.info["MemTotal"] = (floor - 1) * (1 << 20)

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert f"one worker needs at least {floor} MiB" in stderr
    assert fake_docker.calls_of("create") == []
    assert _state_of(spec) == "refused"


def test_an_engine_that_reports_no_memory_cannot_run_the_worker(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    del fake_docker.info["MemTotal"]

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "reported no usable total memory" in stderr
    assert fake_docker.calls_of("create") == []


def test_a_workspace_the_engine_memory_cannot_hold_beside_the_worker_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.info["MemTotal"] = (engine_memory_floor_mib(spec.profile) + 1) * (1 << 20)
    (tmp_path / "workspace" / "big.bin").write_bytes(b"\0" * (2 << 20))

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "cannot be given to the worker" in stderr
    assert "raise the engine virtual machine's memory" in stderr
    assert len(fake_docker.calls_of("create")) == 1  # only the store container was created
    assert fake_docker.containers == {} and fake_docker.volumes == {}
    assert _state_of(spec) == "refused"


def test_a_workspace_that_fits_beside_the_worker_in_the_engine_memory_is_seeded(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    fake_docker.info["MemTotal"] = (engine_memory_floor_mib(spec.profile) + 3) * (1 << 20)
    (tmp_path / "workspace" / "fits.bin").write_bytes(b"\0" * (2 << 20))

    code, _ = _launch(spec, fake_docker)

    assert code == 0


def test_a_returned_workspace_that_cannot_be_taken_back_is_a_fault_not_an_exited_worker(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    profile = RunnerProfile(resources=ResourceLimits(workspace_mib=1))
    spec = make_spec(tmp_path, profile=profile)
    (tmp_path / "workspace" / "kept.txt").write_text("before", encoding="utf-8")

    def at_worker_start(args: tuple[str, ...]) -> None:
        if args[1] == spec.container_name:
            # A volume that did not enforce its cap: the host must not take the oversize tree.
            (fake_docker.workspace_volume(spec) / "big.bin").write_bytes(b"\0" * (2 << 20))

    fake_docker.hooks["start"] = at_worker_start

    code, _ = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert (tmp_path / "workspace" / "kept.txt").read_text() == "before"
    assert sorted(path.name for path in (tmp_path / "workspace").iterdir()) == ["kept.txt"]
    state = json.loads(Path(outcome_path_for(spec.receipt_path)).read_text("utf-8"))
    assert state["state"] == "faulted" and state["exit_code"] is None
    assert fake_docker.containers == {} and fake_docker.volumes == {}


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
    assert fake_docker.verbs[-5:] == ["logs", "rm", "rm", "volume rm", "volume rm"]
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
        (_set_host({"NetworkMode": "bridge"}), "attached only to the network 'none'"),
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
        (
            _set_host({"SecurityOpt": ["no-new-privileges", "seccomp=unconfined"]}),
            "security option 'seccomp=unconfined' is outside the profile",
        ),
        (
            _set_host({"SecurityOpt": ["no-new-privileges", "apparmor=unconfined"]}),
            "security option 'apparmor=unconfined' is outside the profile",
        ),
        (
            _set_host({"SecurityOpt": ["no-new-privileges", "label=disable"]}),
            "security option 'label=disable' is outside the profile",
        ),
        (
            _set_host({"SecurityOpt": ["no-new-privileges", "systempaths=unconfined"]}),
            "security option 'systempaths=unconfined' is outside the profile",
        ),
        (_set_host({"CgroupnsMode": "host"}), "cgroup namespace is 'host'"),
        (_set_host({"MaskedPaths": []}), "kernel paths are not masked"),
        (_set_host({"ReadonlyPaths": []}), "kernel paths are not read-only"),
        (_set_host({"Sysctls": {"kernel.core_pattern": "|/x"}}), "container has sysctls"),
        (_set_host({"Runtime": "nvidia"}), "container runtime is 'nvidia'"),
        (
            _set_host({"Tmpfs": {"/tmp": "rw,nosuid,nodev,exec,size=99999m"}}),
            "scratch space /tmp is not capped",
        ),
        (
            _set_host({"LogConfig": {"Type": "json-file", "Config": {}}}),
            "container log is not rotated",
        ),
        (_set_host({"Ulimits": None}), "per-file size limit is not"),
    ],
)
@pytest.mark.parametrize("target", ["container", "store"])
def test_a_container_weaker_than_the_profile_is_removed_and_never_started_or_recorded(
    fake_docker: FakeDocker,
    tmp_path: Path,
    tamper: Callable[[dict[str, Any]], None],
    violation: str,
    target: str,
) -> None:
    spec = make_spec(tmp_path)
    holder = store_names_for(spec.container_name).holder
    name = spec.container_name if target == "container" else holder
    fake_docker.tamper = lambda document: (
        tamper(document) if document["Name"] == f"/{name}" else None
    )

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert violation in stderr
    assert "profile violation" in stderr
    assert "wait" not in fake_docker.verbs
    # A weak store container is refused before anything starts; a weak worker is refused after
    # the store container started to be seeded, and is never started itself.
    started = [call[1] for call in fake_docker.calls_of("start")]
    assert started == ([] if target == "store" else [holder])
    assert fake_docker.containers == {}
    assert fake_docker.volumes == {}
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
    assert "could not create the store container" in stderr
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
    assert fake_docker.verbs == ["info", "info"]  # the qualification, then the memory headroom


def test_a_verifier_that_answers_for_another_image_is_refused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    def other(_engine: OciEngine, _image_id: str) -> ImageIdentity:
        return image_identity("sha256:" + "7" * 64)

    code, stderr = _launch(make_spec(tmp_path), fake_docker, verify=other)

    assert code == EXIT_FAULT
    assert "not the pinned image" in stderr
    assert "create" not in fake_docker.verbs


def test_an_attempt_network_the_engine_made_routable_is_refused_before_any_container(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path, relay_container="enginery-relay")
    fake_docker.add_relay()
    fake_docker.honor_internal = False  # the engine hands back a network that is not internal

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "not internal" in stderr
    assert "create" not in fake_docker.verbs
    assert "network connect" not in fake_docker.verbs
    assert spec.attempt_network not in fake_docker.networks  # and it is cleaned up again


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
    assert sorted(entry.name for entry in path.parent.iterdir()) == [
        path.name,
        f"{path.name}.outcome.json",
    ]


# --- idempotency ---------------------------------------------------------------------


def test_what_an_earlier_attempt_of_the_same_operation_left_is_replaced(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    names = store_names_for(spec.container_name)
    fake_docker.create_worker(spec)
    fake_docker.calls.clear()

    code, stderr = _launch(spec, fake_docker)

    assert code == 0
    assert "left by an earlier attempt" in stderr
    assert spec.container_name in stderr and names.holder in stderr
    verbs = fake_docker.verbs
    assert verbs.index("rm") < verbs.index("create")
    assert verbs.count("create") == 2  # the store container and the worker, once each
    assert fake_docker.containers == {} and fake_docker.volumes == {}


def test_a_volume_left_without_a_container_is_replaced_not_reused(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    names = store_names_for(spec.container_name)
    fake_docker.create_store(spec)
    fake_docker.engine().remove(names.holder)
    assert set(fake_docker.volumes) == {names.workspace_volume, names.out_volume}

    code, _ = _launch(spec, fake_docker)

    assert code == 0
    assert fake_docker.volumes == {}


def test_a_volume_in_use_by_another_container_is_never_removed_from_under_it(
    fake_docker: FakeDocker, tmp_path: Path
) -> None:
    spec = make_spec(tmp_path)
    names = store_names_for(spec.container_name)
    fake_docker.create_store(spec)
    stranger = fake_docker.containers.pop(names.holder)
    fake_docker.containers["someone-else"] = stranger

    code, stderr = _launch(spec, fake_docker)

    assert code == EXIT_FAULT
    assert "volume rm" in stderr
    assert "someone-else" in fake_docker.containers


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
    assert verbs[-4:] == ["rm", "rm", "volume rm", "volume rm"]
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


@pytest.mark.parametrize("stopped_at", ["store", "worker"])
def test_a_stop_that_arrives_while_a_container_is_created_but_not_started_removes_it(
    fake_docker: FakeDocker, tmp_path: Path, stopped_at: str
) -> None:
    spec = make_spec(tmp_path)
    holder = store_names_for(spec.container_name).holder
    stop = StopSignal()
    target = holder if stopped_at == "store" else spec.container_name

    def at_create(args: tuple[str, ...]) -> None:
        if args[args.index("--name") + 1] == target:
            stop.request(signal.SIGTERM)

    fake_docker.hooks["create"] = at_create

    code, _ = _launch(spec, fake_docker, stop=stop)

    assert code == 143
    assert [call[1] for call in fake_docker.calls_of("start")] == (
        [] if stopped_at == "store" else [holder]
    )
    assert fake_docker.containers == {} and fake_docker.volumes == {}
    assert fake_docker.verbs[-3:] == ["rm", "volume rm", "volume rm"]
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
    assert fake_docker.verbs[-4:] == ["rm", "rm", "volume rm", "volume rm"]
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
    store, worker = fake.calls_of("create")
    assert worker[worker.index("--") + 1 :] == INNER
    assert store[-2:] == ("sleep", "infinity")
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
    overrides.setdefault("permissions", PERMISSIONS_WITHOUT_ARGUMENTS)
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
    assert profile_violations(real_profile, receipt.effective, network="none") == ()
    assert receipt.effective.networks == ("none",)
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


def test_real_launch_never_touches_or_attaches_to_a_same_named_network_it_did_not_create(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    real_operation: Callable[[], str],
    standin_image: str,
    real_root: Path,
) -> None:
    spec = _real_spec(
        real_root,
        real_operation(),
        standin_image,
        real_profile,
        relay_container="enginery-relay-test-absent",
    )
    # A routable network that carries the name this attempt's network would get.
    created = real_engine.run_cli(("network", "create", spec.attempt_network))
    assert created.returncode == 0, created.stderr
    try:
        stream = io.StringIO()

        code = run_launcher(
            spec, INNER, engine=real_engine, verify=_standin_verifier, stderr=stream
        )

        assert code == EXIT_FAULT
        assert "not an attempt network" in stream.getvalue()
        assert real_engine.container_exists(spec.container_name) is False
        assert not os.path.lexists(spec.receipt_path)
        # The network the launcher did not create is left exactly as it was.
        assert real_engine.network_info(spec.attempt_network) is not None
    finally:
        real_engine.run_cli(("network", "rm", spec.attempt_network))


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
