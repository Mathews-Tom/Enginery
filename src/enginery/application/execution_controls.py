"""Native execution-control preparation shared by the harness adapters.

An adapter turns a bound selection into the host's own flags, or rejects it before any
process is launched. Nothing here substitutes: a control that cannot be honored exactly
as selected is an error, never a different model or effort.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import (
    ExecutionCatalog,
    HostCapability,
    InstalledHost,
    RangedHostVersion,
)
from enginery.domain.execution_receipt import (
    EffectiveControl,
    ExecutionReceipt,
    HostObservation,
    RunnerReceiptRef,
)
from enginery.domain.execution_selection import ExecutionSelection, validate_against_catalog


def require_selection_harness(selection: ExecutionSelection, *, harness: str) -> None:
    """Reject a task whose selection names a different harness than the adapter runs."""
    if selection.harness != harness:
        raise InvalidInputError(
            f"the task selects harness {selection.harness!r} but this adapter runs {harness!r}",
            details={"selected_harness": selection.harness, "adapter_harness": harness},
        )


def native_control_arguments(
    selection: ExecutionSelection, *, host: HostCapability
) -> tuple[str, ...]:
    """Return the host-native flags and values for exactly the controls selected."""
    arguments: list[str] = []
    if selection.selected_model is not None:
        arguments += [host.model.flag, selection.selected_model]
    if selection.selected_effort is not None:
        arguments += [host.effort.flag, selection.selected_effort]
    return tuple(arguments)


OBSERVED_HOST_FLAG = "--observed-host-version"
"""Worker-wrapper flag that carries the version observed when the command was built.

The supervised worker ignores it. It exists so that observation (made when the dispatch
command was built, which for queued work is when it was queued, not when the process
later starts) survives in the
durable dispatch record, from which a later collection reads it back instead of probing a
host that may have changed since.
"""


@dataclass(frozen=True, slots=True)
class NativeLaunch:
    """The native control flags of one launch and the host version seen while preparing it."""

    arguments: tuple[str, ...]
    observed_executable_version: str | None


def prepare_native_controls(
    selection: ExecutionSelection,
    *,
    harness: str,
    catalog: ExecutionCatalog,
    probe_installed_host: Callable[[], InstalledHost | None],
) -> NativeLaunch:
    """Validate a selection against the catalog and the installed host, then emit its flags.

    A selection that asks for no native control launches the host exactly as before and is
    never probed. A selection that asks for any control must name the catalog in use,
    carry exact pins in the host's own vocabulary, and run against a host version the catalog
    accepts and the protocol the flags were tested with; otherwise it is rejected before
    launch. The probe that proved the host acceptable is the observation made while the command is
    built (for queued work, when it was queued), and is
    returned with the flags.
    """
    require_selection_harness(selection, harness=harness)
    if selection.legacy or not selection.requires_native_controls:
        return NativeLaunch((), None)
    host = validate_against_catalog(selection, catalog=catalog)
    installed = probe_installed_host()
    host.require_installed(installed)
    return NativeLaunch(
        native_control_arguments(selection, host=host),
        None if installed is None else installed.executable_version,
    )


def launch_observation(command: tuple[str, ...] | None) -> str | None:
    """Return the host version recorded in a persisted supervised command when it was built.

    Only the worker wrapper's own arguments, before the first ``--``, are read; the host
    command that follows is never searched.
    """
    if command is None:
        return None
    wrapper = command[: command.index("--")] if "--" in command else ()
    for index, token in enumerate(wrapper[:-1]):
        if token == OBSERVED_HOST_FLAG and wrapper[index + 1].strip():
            return wrapper[index + 1]
    return None


def expected_native_arguments(
    selection: ExecutionSelection, *, catalog: ExecutionCatalog
) -> tuple[str, ...]:
    """Return exactly the control tokens a launch of this selection must carry."""
    if selection.legacy or not selection.requires_native_controls:
        return ()
    return native_control_arguments(selection, host=catalog.host(selection.harness))


# Flags that steer which model serves a run. A launch carrying one the selection did not
# ask for (for example a fallback model) is not the launch that was selected.
_STEERING_FLAGS = ("--fallback-model", "--provider", "--smol", "--slow", "--plan", "--models")
_MISSING_VALUE = "<missing>"


def extract_native_arguments(command: tuple[str, ...], *, host: HostCapability) -> tuple[str, ...]:
    """Return the model-steering flag/value pairs actually present in a launched command.

    The catalog's model and effort flags and the known model-steering flags are read from
    the host command itself, in order, skipping the prompt that follows ``-p`` and, for a
    supervised command, everything before the first ``--`` separator. This is evidence of
    what was launched, not of what was intended.
    """
    flags = {host.model.flag, host.effort.flag, *_STEERING_FLAGS}
    found: list[str] = []
    # A supervised command is the worker wrapper, its own arguments (an operation id may be
    # spelled like a flag), ``--``, then the host command; only the host command is evidence.
    index = command.index("--") + 1 if "--" in command else 0
    while index < len(command):
        token = command[index]
        if token == "-p":
            index += 2
            continue
        if token in flags:
            value = command[index + 1] if index + 1 < len(command) else _MISSING_VALUE
            found += [token, value]
            index += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in flags):
            found.append(token)
        index += 1
    return tuple(found)


def build_receipt(
    selection: ExecutionSelection,
    *,
    harness: str,
    catalog: ExecutionCatalog,
    launched_command: tuple[str, ...] | None,
    models: Iterable[str],
    observed_at_launch: str | None,
    runner: RunnerReceiptRef | None = None,
) -> ExecutionReceipt:
    """Build a receipt from the launched command and the host's own reported models.

    Submitted tokens are extracted from the command that was launched. The effective model's
    evidence description comes from the catalog; effective effort has no extractor, so it is
    unknown and says whether the catalog records any evidence for it. A selection that asks
    for a native control also records the host versions it answers: the bound range, the
    tested version and the version observed when the dispatch command was built, never one probed
    later and never reported as tested.
    """
    require_selection_harness(selection, harness=harness)
    host = catalog.host(harness)
    submitted = (
        () if launched_command is None else extract_native_arguments(launched_command, host=host)
    )
    if host.model.effective_evidence:
        model = distinct_model(
            models,
            source=host.model.effective_evidence,
            reason=f"no {harness} event reported a model ({host.model.effective_evidence})",
        )
    else:
        model = EffectiveControl.unknown(
            reason=f"the catalog records no effective-model evidence for {harness}"
        )
    effort_reason = (
        f"the catalog records no effective-effort evidence for {harness}"
        if not host.effort.effective_evidence
        else f"effective-effort evidence '{host.effort.effective_evidence}' is not extracted"
    )
    return ExecutionReceipt(
        selection_digest=None if selection.legacy else selection.digest,
        harness=harness,
        submitted_arguments=submitted,
        effective_model=model,
        effective_effort=EffectiveControl.unknown(reason=effort_reason),
        host=_host_observation(selection, observed_at_launch),
        runner=runner,
    )


def _host_observation(
    selection: ExecutionSelection, observed_at_launch: str | None
) -> HostObservation | None:
    version = selection.host_version
    if selection.legacy or version is None or not selection.requires_native_controls:
        return None
    if isinstance(version, RangedHostVersion):
        return HostObservation(
            accepted_executable_versions=str(version.accepted),
            tested_executable_version=str(version.tested),
            observed_executable_version_at_launch=observed_at_launch,
        )
    return HostObservation(
        accepted_executable_versions=None,
        tested_executable_version=version.version,
        observed_executable_version_at_launch=observed_at_launch,
    )


def distinct_model(models: Iterable[str], *, source: str, reason: str) -> EffectiveControl:
    """Fold observed model identifiers into one effective control.

    No observation is UNKNOWN. A single identifier is observed. Several distinct
    identifiers are reported together rather than collapsed, so a pin never appears to
    match when the host also served another model.
    """
    observed = sorted(set(models))
    if not observed:
        return EffectiveControl.unknown(reason=reason)
    return EffectiveControl.observed(",".join(observed), source=source)


__all__ = [
    "OBSERVED_HOST_FLAG",
    "NativeLaunch",
    "build_receipt",
    "distinct_model",
    "expected_native_arguments",
    "extract_native_arguments",
    "launch_observation",
    "native_control_arguments",
    "prepare_native_controls",
    "require_selection_harness",
]
