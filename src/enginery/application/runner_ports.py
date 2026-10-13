"""Ports through which workflows ask whether an isolated runner can actually be used."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol

from enginery.domain.execution_receipt import RunnerReceiptRef
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


class AttestedProcess(CompletedProcess[str]):
    """A finished isolated command plus the verified runner receipt it ran under.

    The isolated validation runner returns this instead of a bare ``CompletedProcess`` so the
    validation result can name the retained receipt (image, profile, network none) that proves
    what the candidate's tests ran under.
    """

    runner_receipt: RunnerReceiptRef

    def __init__(
        self,
        args: tuple[str, ...],
        returncode: int,
        stdout: str,
        stderr: str,
        *,
        runner_receipt: RunnerReceiptRef,
    ) -> None:
        super().__init__(args, returncode, stdout, stderr)
        self.runner_receipt = runner_receipt


IsolatedCommandRunner = Callable[[tuple[str, ...], Path], CompletedProcess[str]]
"""Run one command in the isolated runner against a task-local workspace.

The command runs under the same restrictions as the worker: no credentials, no host
filesystem beyond the workspace and no network at all. A runner that can prove what the command
ran under returns an ``AttestedProcess``.
"""


__all__ = [
    "AttestedProcess",
    "IsolatedCommandRunner",
    "RunnerAdmissionDecision",
    "RunnerAdmissionPort",
]
