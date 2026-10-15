"""Remove runner containers, attempt networks and volumes whose launcher process is gone.

Every worker container is created by a launcher process that waits for it and removes it. If
that launcher is killed outright (SIGKILL after a stop grace, a crash, a reboot) the container
keeps running with nothing supervising it, and the internal network the launcher created for the
attempt (with the relay still attached to it, for a worker with model access) is left behind. The
launcher labels each container and each attempt network with its own pid, start identity and
host; reconcile lists them by label and removes those whose launcher is provably gone. One whose
launcher is still the same live process is never touched, and one that cannot be attributed is
reported, not removed. The capped volumes of an attempt are created with its store container, so
once their containers are gone they are dangling and are removed too.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from enginery.adapters.oci.engine import LAUNCHER_LABEL, EngineCommandError, OciEngine
from enginery.domain.errors import EngineryError
from enginery.engine.supervisor import (
    HostIdentity,
    Presence,
    ProcessIdentity,
    probe_host,
    probe_presence,
)

PresenceProbe = Callable[[ProcessIdentity], Presence]
HostProbe = Callable[[], HostIdentity]


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one reconcile pass did to the engine's runner containers and attempt networks."""

    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    """Containers whose launcher is still alive."""
    unattributed: tuple[str, ...] = ()
    """Containers that name no usable launcher, or one on another host: left alone."""
    failed: tuple[tuple[str, str], ...] = field(default=())
    """``(name, reason)`` for containers or volumes that could not be inspected or removed."""
    volumes_removed: tuple[str, ...] = ()
    """Runner volumes no container referred to any more."""
    networks_removed: tuple[str, ...] = ()
    networks_kept: tuple[str, ...] = ()
    """Attempt networks whose launcher is still alive."""
    networks_unattributed: tuple[str, ...] = ()
    networks_failed: tuple[tuple[str, str], ...] = field(default=())

    @property
    def clean(self) -> bool:
        return not self.failed and not self.networks_failed


def _launcher_identity(labels: Mapping[str, object]) -> tuple[ProcessIdentity, str, str] | None:
    raw = labels.get(LAUNCHER_LABEL)
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid, pgid, start = data.get("pid"), data.get("pgid"), data.get("start")
    machine, boot = data.get("machine"), data.get("boot")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(pgid, int)
        or isinstance(pgid, bool)
        or not isinstance(start, str)
        or not start
        or not isinstance(machine, str)
        or not isinstance(boot, str)
    ):
        return None
    return ProcessIdentity(pid, pgid, start, HostIdentity(machine, boot)), machine, boot


_ORPHANED = "orphaned"
_ALIVE = "alive"
_UNATTRIBUTED = "unattributed"


def _judge(labels: Mapping[str, object], *, here: HostIdentity, probe: PresenceProbe) -> str:
    """Whether the launcher named by ``labels`` is gone, alive, or cannot be judged from here.

    A launcher on another machine cannot be judged and its objects are left alone; one recorded
    for an earlier boot of this machine is gone by definition, whatever its pid is now. Raises
    ``EngineryError`` when the launcher's presence cannot be established.
    """
    found = _launcher_identity(labels)
    if found is None:
        return _UNATTRIBUTED
    identity, machine, boot = found
    if machine != here.machine_id:
        return _UNATTRIBUTED
    if boot == here.boot_id and probe(identity) is Presence.SAME:
        return _ALIVE
    return _ORPHANED


def reconcile_runner_containers(
    engine: OciEngine,
    *,
    keep: frozenset[str] = frozenset(),
    probe: PresenceProbe = probe_presence,
    host: HostProbe = probe_host,
) -> ReconcileReport:
    """Remove runner containers (by label) whose launcher process is gone.

    ``keep`` names containers never to touch (the caller's own). A launcher on another machine
    cannot be judged from here and its container is left alone; one recorded for an earlier
    boot of this machine is gone by definition, whatever its pid is now.
    """
    here = host()
    removed: list[str] = []
    kept: list[str] = []
    unattributed: list[str] = []
    failed: list[tuple[str, str]] = []
    for name in engine.list_runner_containers():
        if name in keep:
            kept.append(name)
            continue
        try:
            labels = _map(_map(engine.container_inspect(name).get("Config")).get("Labels"))
        except EngineCommandError as error:
            failed.append((name, str(error)))
            continue
        try:
            verdict = _judge(labels, here=here, probe=probe)
        except EngineryError as error:  # the launcher's presence cannot be established
            failed.append((name, str(error)))
            continue
        if verdict == _UNATTRIBUTED:
            unattributed.append(name)
            continue
        if verdict == _ALIVE:
            kept.append(name)
            continue
        try:
            engine.remove(name, force=True)
        except EngineryError as error:
            failed.append((name, str(error)))
            continue
        removed.append(name)
    volumes_removed: list[str] = []
    try:
        dangling = engine.list_dangling_runner_volumes()
    except EngineryError as error:
        failed.append(("volumes", str(error)))
        dangling = ()
    # A volume is created together with the container that first refers to it, so a dangling
    # runner volume belongs to a container that is gone (removed just now or earlier).
    for volume in dangling:
        try:
            engine.volume_remove(volume)
        except EngineryError as error:
            failed.append((volume, str(error)))
            continue
        volumes_removed.append(volume)
    return ReconcileReport(
        removed=tuple(removed),
        kept=tuple(kept),
        unattributed=tuple(unattributed),
        failed=tuple(failed),
        volumes_removed=tuple(volumes_removed),
    )


def reconcile_attempt_networks(
    engine: OciEngine,
    *,
    keep: frozenset[str] = frozenset(),
    probe: PresenceProbe = probe_presence,
    host: HostProbe = probe_host,
) -> ReconcileReport:
    """Remove attempt networks (by label) whose launcher process is gone.

    A network whose launcher died keeps whatever was attached to it, normally the relay: the
    relay is disconnected (it is never removed) and the network is deleted. ``keep`` names
    networks never to touch (the caller's own). Run after the container pass, so a worker whose
    launcher is gone is already removed when its network goes.
    """
    here = host()
    removed: list[str] = []
    kept: list[str] = []
    unattributed: list[str] = []
    failed: list[tuple[str, str]] = []
    for name in engine.list_attempt_networks():
        if name in keep:
            kept.append(name)
            continue
        try:
            info = engine.network_info(name)
        except EngineCommandError as error:
            failed.append((name, str(error)))
            continue
        if info is None:  # removed by its own launcher since the listing
            continue
        try:
            verdict = _judge(info.labels, here=here, probe=probe)
        except EngineryError as error:
            failed.append((name, str(error)))
            continue
        if verdict == _UNATTRIBUTED:
            unattributed.append(name)
            continue
        if verdict == _ALIVE:
            kept.append(name)
            continue
        try:
            engine.remove_attempt_network(name)
        except EngineryError as error:
            failed.append((name, str(error)))
            continue
        removed.append(name)
    return ReconcileReport(
        networks_removed=tuple(removed),
        networks_kept=tuple(kept),
        networks_unattributed=tuple(unattributed),
        networks_failed=tuple(failed),
    )


def reconcile_runner(
    engine: OciEngine,
    *,
    keep_containers: frozenset[str] = frozenset(),
    keep_networks: frozenset[str] = frozenset(),
    probe: PresenceProbe = probe_presence,
    host: HostProbe = probe_host,
) -> ReconcileReport:
    """Reconcile the runner's containers, then its attempt networks, and report both."""
    containers = reconcile_runner_containers(engine, keep=keep_containers, probe=probe, host=host)
    networks = reconcile_attempt_networks(engine, keep=keep_networks, probe=probe, host=host)
    return ReconcileReport(
        removed=containers.removed,
        kept=containers.kept,
        unattributed=containers.unattributed,
        failed=containers.failed,
        volumes_removed=containers.volumes_removed,
        networks_removed=networks.networks_removed,
        networks_kept=networks.networks_kept,
        networks_unattributed=networks.networks_unattributed,
        networks_failed=networks.networks_failed,
    )


def _map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
