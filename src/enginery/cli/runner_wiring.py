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
from enginery.adapters.oci.launch import worker_container_gone
from enginery.adapters.oci.probe import OciProbeRunner
from enginery.adapters.oci.validation import OciValidationRunner
from enginery.application.runner_ports import IsolatedCommandRunner, RunnerAdmissionPort
from enginery.domain.runner import (
    MODEL_ACCESS_RELAY,
    RelayEndpoint,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
)
from enginery.workflows.run_intent import Stage1RunRequest

_RECEIPT_DIRECTORY = "runner-receipts"


@dataclass(frozen=True, slots=True)
class IsolatedWiring:
    """Everything an isolated run needs beyond a trusted-local one."""

    context: RunnerContext
    probe: Callable[[tuple[str, ...]], CompletedProcess[str]]
    admission: RunnerAdmissionPort
    validation: IsolatedCommandRunner


@dataclass(frozen=True, slots=True)
class DriftedRunnerWiring:
    """A run bound to a runner profile that is no longer the current one.

    Only the admission is wired, so the run can be diagnosed and its hold reported with the
    retained binding intact. There is no context, probe or validation runner, so nothing can
    be launched under restrictions the run was not bound to.
    """

    admission: RunnerAdmissionPort


def isolated_wiring(
    request: Stage1RunRequest, *, create_directories: bool
) -> IsolatedWiring | DriftedRunnerWiring | None:
    """Return the isolated wiring for a run, or None for a trusted-local run.

    A bound profile that is not the current one yields admission-only wiring: the service then
    reports the same ``runner_fingerprint_drift`` hold admission reports, in every command that
    reads the run, and no launch is possible. ``create_directories`` makes the receipt directory
    only for an advancing service; a read-only diagnostic never creates harness state.
    """
    selection = request.runner
    if not selection.isolated_mode or selection.image is None:
        return None
    profile = RunnerProfile()
    if selection.profile_digest != profile.digest:
        return DriftedRunnerWiring(admission=OciRunnerAdmission(profile=profile))
    receipt_root = request.execution_configuration.artifact_root / _RECEIPT_DIRECTORY
    if create_directories:
        receipt_root.mkdir(parents=True, exist_ok=True)
    context = RunnerContext(
        selection=selection,
        profile=profile,
        receipt_root=str(receipt_root),
        relay=RelayEndpoint() if selection.model_access == MODEL_ACCESS_RELAY else None,
    )
    engine = OciEngine(context=selection.docker_context)
    return IsolatedWiring(
        context=context,
        probe=OciProbeRunner(engine, selection.image, profile),
        admission=OciRunnerAdmission(profile=profile),
        validation=OciValidationRunner(
            context, artifact_root=request.execution_configuration.artifact_root
        ),
    )


def container_gone_for(selection: RunnerSelection) -> Callable[[str], bool] | None:
    """Return the engine lookup recovery uses to prove an isolated worker's container is gone.

    ``None`` for a trusted-local run, whose worker is a host process recovery already checks.
    For an isolated run the lookup asks the run's own engine (its bound docker context) whether
    the deterministically named container of an operation still exists, running or not. A
    lookup that cannot reach the engine raises, which recovery treats as unverifiable.
    """
    if not selection.isolated_mode:
        return None
    engine = OciEngine(context=selection.docker_context)
    return lambda operation_id: worker_container_gone(engine, operation_id)


__all__ = ["DriftedRunnerWiring", "IsolatedWiring", "container_gone_for", "isolated_wiring"]
