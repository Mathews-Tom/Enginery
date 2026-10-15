"""The gate's cleanup and stray accounting, against a fake container engine.

A gate run must leave nothing on the engine: not the workers, not the per-attempt store containers
that hold the capped volumes, not the volumes, not the networks. These tests drive ``Gate`` with an
engine that keeps its objects in memory and understands the few ``docker`` commands the gate uses,
so what the gate removes, what it reports as a leak and what it refuses to call clean is decided
by the gate's own code and nothing else. No container is started.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import store_names_for
from factory_gate_context import Gate

RID = "feedface"
WORKER = "enginery-0123456789abcdef"
OTHER = "enginery-fedcba9876543210"
ATTEMPT_NETWORK = f"enginery-gate-{RID}-internal-aaaaaaaaaaaaaaaaaaaaaaaa"


class FakeEngine:
    """An in-memory ``docker``: containers, volumes and networks, plus injectable failures."""

    def __init__(self) -> None:
        self.containers: set[str] = set()
        self.volumes: set[str] = set()
        self.networks: set[str] = set()
        self.refuse: set[str] = set()  # command prefixes that exit 1, e.g. "volume ls"
        self.hang: set[str] = set()  # whole commands that time out, e.g. "network rm gate-a"
        self.calls: list[tuple[str, ...]] = []

    def engine(self) -> OciEngine:
        return OciEngine(runner=self._run)

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        input: str | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        args = tuple(argv[1:])
        self.calls.append(args)
        text = " ".join(args)
        if text in self.hang:
            raise subprocess.TimeoutExpired(list(argv), timeout or 0)
        if any(text.startswith(prefix) for prefix in self.refuse):
            return subprocess.CompletedProcess(list(argv), 1, "", "Error: the daemon refused")
        if args[:2] == ("ps", "--all"):
            return self._ok("\n".join(sorted(self.containers)))
        if args[:2] == ("volume", "ls"):
            return self._ok("\n".join(sorted(self.volumes)))
        if args[:2] == ("network", "ls"):
            return self._ok("\n".join(sorted(self.networks)))
        if args[0] == "rm":
            self.containers.discard(args[-1])
            return self._ok("")
        if args[:2] == ("volume", "rm"):
            return self._remove(self.volumes, args[-1], "no such volume")
        if args[:2] == ("network", "rm"):
            return self._remove(self.networks, args[-1], "network not found")
        raise AssertionError(f"the fake engine does not know {text!r}")

    @staticmethod
    def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout + "\n", "")

    @staticmethod
    def _remove(objects: set[str], name: str, absent: str) -> subprocess.CompletedProcess[str]:
        if name not in objects:
            return subprocess.CompletedProcess([], 1, "", f"Error response from daemon: {absent}")
        objects.discard(name)
        return subprocess.CompletedProcess([], 0, "", "")

    def add_attempt(self, worker: str, *, store: bool, volumes: bool) -> None:
        names = store_names_for(worker)
        self.containers.add(worker)
        if store:
            self.containers.add(names.holder)
        if volumes:
            self.volumes.update((names.workspace_volume, names.out_volume))


def make_gate(fake: FakeEngine, tmp_path: Path) -> Gate:
    gate = Gate(
        options=SimpleNamespace(docker_context=None),  # type: ignore[arg-type]
        engine=fake.engine(),
        evidence=None,  # type: ignore[arg-type]
        scrubber=None,  # type: ignore[arg-type]
        engine_identity=None,  # type: ignore[arg-type]
        runner_image=None,  # type: ignore[arg-type]
        relay_image=None,
        workroot=tmp_path / "work",
        rid=RID,
    )
    gate.track_container(WORKER)
    gate.track_network(gate.internal_network)
    gate.track_network(ATTEMPT_NETWORK, per_attempt=True)
    return gate


def test_the_store_container_and_volumes_of_a_worker_are_strays_until_removed(
    tmp_path: Path,
) -> None:
    fake = FakeEngine()
    fake.add_attempt(WORKER, store=True, volumes=True)
    fake.add_attempt(OTHER, store=True, volumes=True)  # another operator's, never ours
    fake.networks.update((f"enginery-gate-{RID}-internal", ATTEMPT_NETWORK, "enginery-gate-other"))
    gate = make_gate(fake, tmp_path)
    names = store_names_for(WORKER)

    strays = gate.stray_objects()

    assert strays.containers == tuple(sorted((WORKER, names.holder)))
    assert strays.volumes == tuple(sorted((names.workspace_volume, names.out_volume)))
    assert strays.networks == tuple(sorted((f"enginery-gate-{RID}-internal", ATTEMPT_NETWORK)))
    assert not strays.clean


def test_cleanup_removes_what_the_launcher_left_and_reports_it(tmp_path: Path) -> None:
    fake = FakeEngine()
    fake.add_attempt(WORKER, store=True, volumes=True)
    fake.add_attempt(OTHER, store=True, volumes=True)
    fake.networks.update((f"enginery-gate-{RID}-internal", ATTEMPT_NETWORK, "enginery-gate-other"))
    gate = make_gate(fake, tmp_path)
    names = store_names_for(WORKER)

    gate.cleanup()

    report = gate.cleanup_report
    assert gate.stray_objects().clean
    # Another operator's attempt and network are untouched.
    store_other = store_names_for(OTHER)
    assert {OTHER, store_other.holder} <= fake.containers
    assert {store_other.workspace_volume, store_other.out_volume} <= fake.volumes
    assert fake.networks == {"enginery-gate-other"}
    assert sorted(report["containers_removed"]) == sorted((WORKER, names.holder))
    assert sorted(report["volumes_removed"]) == sorted((names.workspace_volume, names.out_volume))
    assert sorted(report["networks_removed"]) == sorted(
        (f"enginery-gate-{RID}-internal", ATTEMPT_NETWORK)
    )
    # What the launcher should have removed itself is a defect even though the gate cleaned it up;
    # the worker and the gate's own scratch network are the gate's to remove, not a leak.
    errors = "\n".join(report["errors"])
    for leaked in (names.holder, names.workspace_volume, names.out_volume, ATTEMPT_NETWORK):
        assert f"{leaked} was left behind by the launcher" in errors
    assert f"container {WORKER} was left" not in errors
    assert f"enginery-gate-{RID}-internal was left" not in errors


def test_a_clean_launcher_leaves_nothing_for_the_gate_to_report(tmp_path: Path) -> None:
    fake = FakeEngine()
    fake.containers.add(WORKER)  # the worker is the gate's to remove; its store is already gone
    fake.networks.add(f"enginery-gate-{RID}-internal")
    gate = make_gate(fake, tmp_path)

    gate.cleanup()

    assert gate.cleanup_report["errors"] == []
    assert gate.stray_objects().clean
    assert not fake.containers and not fake.volumes and not fake.networks


@pytest.mark.parametrize("listing", ["ps --all", "volume ls", "network ls"])
def test_a_failed_listing_is_never_a_clean_stray_check(tmp_path: Path, listing: str) -> None:
    fake = FakeEngine()
    fake.refuse.add(listing)
    gate = make_gate(fake, tmp_path)

    strays = gate.stray_objects()
    gate.cleanup()

    assert not strays.clean
    assert strays.listing_errors
    assert gate.cleanup_report["errors"]


def test_cleanup_continues_past_a_command_that_hangs(tmp_path: Path) -> None:
    fake = FakeEngine()
    fake.networks.update((f"enginery-gate-{RID}-internal", ATTEMPT_NETWORK))
    fake.hang.add(f"network rm enginery-gate-{RID}-internal")
    gate = make_gate(fake, tmp_path)

    gate.cleanup()  # must not raise: the next network and the rest of the cleanup still happen

    assert fake.networks == {f"enginery-gate-{RID}-internal"}
    assert gate.cleanup_report["networks_removed"] == [ATTEMPT_NETWORK]
    assert any(
        f"network enginery-gate-{RID}-internal" in error for error in gate.cleanup_report["errors"]
    )


def test_a_validation_container_the_product_launched_is_tracked_from_its_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation = "enginery-00000000000000aa"
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "op.validation-receipt.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "factory_gate_context.read_receipt",
        lambda _path: (SimpleNamespace(container_name=validation), {}),
    )
    fake = FakeEngine()
    fake.add_attempt(validation, store=True, volumes=True)
    gate = make_gate(fake, tmp_path)
    work = tmp_path / "fixture-work"
    work.mkdir()  # cleanup also sweeps the task workspaces under it; there are none
    gate.fixture = SimpleNamespace(receipts=receipts, work=work)  # type: ignore[assignment]
    names = store_names_for(validation)

    strays = gate.stray_objects()

    assert {validation, names.holder} <= set(strays.containers)
    assert {names.workspace_volume, names.out_volume} <= set(strays.volumes)
    assert not strays.clean
    gate.cleanup()
    assert not fake.containers
    assert not fake.volumes
