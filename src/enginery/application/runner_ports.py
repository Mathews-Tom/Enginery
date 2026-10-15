"""Ports through which workflows ask whether an isolated runner can actually be used."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol

from enginery.domain.execution_selection import ExecutionHold
from enginery.domain.runner import EngineIdentity, ImageIdentity, RunnerSelection


@dataclass(frozen=True, slots=True)
class RunnerAdmissionDecision:
    """Whether a selection can run: either a hold naming why not, or the proven identities.

    A selection that requires isolation is never admitted on a weaker mode. ``hold`` is
    None only when the engine was reached and qualified and the pinned image and profile
    were verified; ``engine`` and ``image`` then say exactly what was verified.
    """

    hold: ExecutionHold | None
    engine: EngineIdentity | None = None
    image: ImageIdentity | None = None


class RunnerAdmissionPort(Protocol):
    """Qualify the engine, image and profile an isolated selection is pinned to."""

    def admit(self, selection: RunnerSelection) -> RunnerAdmissionDecision: ...


IsolatedCommandRunner = Callable[[tuple[str, ...], Path], CompletedProcess[str]]
"""Run one command in the isolated runner against a task-local workspace.

The command runs under the same restrictions as the worker: no credentials, no host
filesystem beyond the workspace, no route to anything but the internal runner network.
"""


__all__ = ["IsolatedCommandRunner", "RunnerAdmissionDecision", "RunnerAdmissionPort"]
