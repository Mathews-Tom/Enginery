"""Native execution-control preparation shared by the harness adapters.

An adapter turns a bound selection into the host's own flags, or rejects it before any
process is launched. Nothing here substitutes: a control that cannot be honored exactly
as selected is an error, never a different model or effort.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import ExecutionCatalog, HostCapability, InstalledHost
from enginery.domain.execution_receipt import EffectiveControl, ExecutionReceipt
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


def prepare_native_controls(
    selection: ExecutionSelection,
    *,
    harness: str,
    catalog: ExecutionCatalog,
    probe_installed_host: Callable[[], InstalledHost | None],
) -> tuple[str, ...]:
    """Validate a selection against the catalog and the installed host, then emit its flags.

    A selection that asks for no native control launches the host exactly as before and is
    never probed. A selection that asks for any control must name the catalog in use,
    carry exact pins in the host's own vocabulary, and run against the host version and
    protocol the flags were tested with; otherwise it is rejected before launch.
    """
    require_selection_harness(selection, harness=harness)
    if selection.legacy or not selection.requires_native_controls:
        return ()
    host = validate_against_catalog(selection, catalog=catalog)
    host.require_installed(probe_installed_host())
    return native_control_arguments(selection, host=host)


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
    the command itself, in order, skipping the prompt that follows ``-p``. This is evidence
    of what was launched, not of what was intended.
    """
    flags = {host.model.flag, host.effort.flag, *_STEERING_FLAGS}
    found: list[str] = []
    index = 0
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
) -> ExecutionReceipt:
    """Build a receipt from the launched command and the host's own reported models.

    Submitted tokens are extracted from the command that was launched. The effective model's
    evidence description comes from the catalog; effective effort has no extractor, so it is
    unknown and says whether the catalog records any evidence for it.
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
    "build_receipt",
    "distinct_model",
    "expected_native_arguments",
    "extract_native_arguments",
    "native_control_arguments",
    "prepare_native_controls",
    "require_selection_harness",
]
