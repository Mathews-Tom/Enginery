"""Admit an isolated selection only when a qualified engine, image and profile back it.

Admission is the fail-closed gate between "the run asked for isolation" and "a worker may be
started in a container". It never degrades: an engine that cannot enforce the profile, an image
that is missing or built from a different recipe, or a profile that is not the one the run was
bound to is a named hold, and the run is not run in any weaker mode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import verify_image
from enginery.adapters.oci.launch import engine_memory_shortfall
from enginery.application.runner_ports import RunnerAdmissionDecision
from enginery.domain.errors import (
    MissingPrerequisiteError,
    OperationTimeoutError,
    StaleEvidenceError,
)
from enginery.domain.execution_selection import ExecutionHold, HoldReason
from enginery.domain.runner import ImageIdentity, RunnerProfile, RunnerSelection


def _default_engine(context: str | None) -> OciEngine:
    return OciEngine(context=context)


@dataclass(frozen=True, slots=True)
class OciRunnerAdmission:
    """Qualify the engine, the pinned image and the pinned profile of one selection."""

    engine_factory: Callable[[str | None], OciEngine] = _default_engine
    verify: Callable[[OciEngine, str], ImageIdentity] = verify_image
    profile: RunnerProfile = field(default_factory=RunnerProfile)

    def admit(self, selection: RunnerSelection) -> RunnerAdmissionDecision:
        if not selection.isolated_mode:
            return RunnerAdmissionDecision(hold=None)
        if selection.profile_digest != self.profile.digest:
            return _drift(
                "the run is bound to a runner profile that is not the current one; it is held, "
                "not run under different restrictions",
                bound=str(selection.profile_digest),
                current=str(self.profile.digest),
            )
        image_id = selection.image
        if image_id is None:  # a validated isolated selection always pins an image
            return _unqualified("the isolated selection pins no image")
        engine = self.engine_factory(selection.docker_context)
        try:
            identity = engine.qualify()
        except (MissingPrerequisiteError, OperationTimeoutError) as error:
            return _unqualified(f"no qualified container engine is available: {error}")
        try:
            shortfall = engine_memory_shortfall(self.profile, engine.memory_total_bytes())
        except (MissingPrerequisiteError, OperationTimeoutError) as error:
            return _unqualified(f"the container engine's memory cannot be checked: {error}")
        if shortfall is not None:
            return _unqualified(shortfall)
        try:
            image = self.verify(engine, image_id)
        except StaleEvidenceError as error:
            return _drift(
                "the pinned worker image was not built from the current runner recipe; it is "
                "held, not run on a different image",
                **{key: str(value) for key, value in error.details.items()},
            )
        except MissingPrerequisiteError as error:
            return _unqualified(f"the pinned worker image is not usable on this engine: {error}")
        return RunnerAdmissionDecision(hold=None, engine=identity, image=image)


def _unqualified(message: str) -> RunnerAdmissionDecision:
    return RunnerAdmissionDecision(
        hold=ExecutionHold(
            reason=HoldReason.RUNNER_UNQUALIFIED,
            message=f"{message}; the run is held, not run in trusted-local mode",
        )
    )


def _drift(message: str, **details: str) -> RunnerAdmissionDecision:
    return RunnerAdmissionDecision(
        hold=ExecutionHold(
            reason=HoldReason.RUNNER_FINGERPRINT_DRIFT, message=message, details=details
        )
    )


__all__ = ["OciRunnerAdmission"]
