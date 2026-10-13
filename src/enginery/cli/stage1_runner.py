"""Compose the isolated runner's engine-facing pieces for the Stage 1 lifecycle commands.

A run bound to ``trusted-local`` needs none of this. A run bound to ``isolated-oci`` gets the
runner context its harness launches under, the admission that qualifies the engine, image and
profile, the in-image probe the harness fingerprint is read through, and the runner-confined
validation command runner. Nothing here ever substitutes a weaker mode: a run whose bound profile
is no longer the current one is held, not run under different restrictions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from subprocess import CompletedProcess

from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.probe import OciProbeRunner
from enginery.adapters.oci.validation import OciValidationRunner
from enginery.application.runner_ports import IsolatedCommandRunner, RunnerAdmissionPort
from enginery.domain.errors import HumanActionRequiredError
from enginery.domain.runner import RunnerContext, RunnerProfile
from enginery.workflows.stage1 import Stage1RunRequest

_RECEIPT_DIRECTORY = "runner-receipts"


@dataclass(frozen=True, slots=True)
class IsolatedWiring:
    """Everything an isolated run needs beyond a trusted-local one."""

    context: RunnerContext
    probe: Callable[[tuple[str, ...]], CompletedProcess[str]]
    admission: RunnerAdmissionPort
    validation: IsolatedCommandRunner


def isolated_wiring(
    request: Stage1RunRequest, *, create_directories: bool
) -> IsolatedWiring | None:
    """Return the isolated wiring for a run, or None for a trusted-local run.

    A bound profile that is not the current one raises the same hold admission reports, so a
    diagnostic and a launch agree and neither runs the worker under different restrictions.
    ``create_directories`` makes the receipt directory only for an advancing service; a
    read-only diagnostic never creates harness state.
    """
    selection = request.runner
    if not selection.isolated_mode or selection.image is None:
        return None
    profile = RunnerProfile()
    if selection.profile_digest != profile.digest:
        raise HumanActionRequiredError(
            "the run is bound to a runner profile that is not the current one; it is held, not "
            "run under different restrictions",
            details={
                "bound_profile_digest": str(selection.profile_digest),
                "current_profile_digest": str(profile.digest),
            },
        )
    receipt_root = request.execution_configuration.artifact_root / _RECEIPT_DIRECTORY
    if create_directories:
        receipt_root.mkdir(parents=True, exist_ok=True)
    context = RunnerContext(selection=selection, profile=profile, receipt_root=str(receipt_root))
    engine = OciEngine(context=selection.docker_context)
    return IsolatedWiring(
        context=context,
        probe=OciProbeRunner(engine, selection.image, profile),
        admission=OciRunnerAdmission(profile=profile),
        validation=OciValidationRunner(context),
    )


__all__ = ["IsolatedWiring", "isolated_wiring"]
