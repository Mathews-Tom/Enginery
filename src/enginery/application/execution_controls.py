"""Native execution-control preparation shared by the harness adapters.

An adapter turns a bound selection into the host's own flags, or rejects it before any
process is launched. Nothing here substitutes: a control that cannot be honored exactly
as selected is an error, never a different model or effort.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import ExecutionCatalog, HostCapability, InstalledHost
from enginery.domain.execution_receipt import EffectiveControl
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


def submitted_arguments(
    selection: ExecutionSelection, *, harness: str, catalog: ExecutionCatalog
) -> tuple[str, ...]:
    """Return the control tokens a launch of this selection emits, for the receipt."""
    require_selection_harness(selection, harness=harness)
    if selection.legacy or not selection.requires_native_controls:
        return ()
    return native_control_arguments(selection, host=catalog.host(harness))


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
    "distinct_model",
    "native_control_arguments",
    "prepare_native_controls",
    "require_selection_harness",
    "submitted_arguments",
]
