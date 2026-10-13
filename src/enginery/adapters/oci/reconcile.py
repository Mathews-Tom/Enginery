"""Remove runner containers whose launcher process is gone.

Every worker container is created by a launcher process that waits for it and removes it. If
that launcher is killed outright (SIGKILL after a stop grace, a crash, a reboot) the container
keeps running with nothing supervising it. The launcher labels each container with its own pid,
start identity and host; reconcile lists every runner container by label and removes those
whose launcher is provably gone. A container whose launcher is still the same live process is
never touched, and one that cannot be attributed is reported, not removed.
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
    """What one reconcile pass did to the engine's runner containers."""

    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    """Containers whose launcher is still alive."""
    unattributed: tuple[str, ...] = ()
    """Containers that name no usable launcher, or one on another host: left alone."""
    failed: tuple[tuple[str, str], ...] = field(default=())
    """``(name, reason)`` for containers that could not be inspected or removed."""


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
        found = _launcher_identity(labels)
        if found is None:
            unattributed.append(name)
            continue
        identity, machine, boot = found
        if machine != here.machine_id:
            unattributed.append(name)
            continue
        if boot == here.boot_id:
            try:
                alive = probe(identity) is Presence.SAME
            except EngineryError as error:  # the launcher's presence cannot be established
                failed.append((name, str(error)))
                continue
            if alive:
                kept.append(name)
                continue
        try:
            engine.remove(name, force=True)
        except EngineryError as error:
            failed.append((name, str(error)))
            continue
        removed.append(name)
    return ReconcileReport(
        removed=tuple(removed),
        kept=tuple(kept),
        unattributed=tuple(unattributed),
        failed=tuple(failed),
    )


def _map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
