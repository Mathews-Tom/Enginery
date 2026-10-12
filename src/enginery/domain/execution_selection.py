"""Typed execution selection: which harness, model, and native effort a run requested.

A selection is bound before any worker launches and records, separately, what was
requested (with the layer that supplied each value), what was selected, how the
selection was made, and the exact catalog revision it was resolved against. What a
host was actually *sent* and what it *effectively ran* are runtime observations kept
apart from the selection; they are never derived from, or defaulted to, the request.

Precedence is run > project > user > workflow > factory: the highest layer that
specifies a control wins. A manual pin marks the winning value exact: it is never
fuzzy-matched, aliased, or replaced, and a pin at a lower layer that disagrees with
a higher layer's value is rejected rather than resolved either way. Model and effort
are host-specific, so a layer that names another harness contributes neither, and a
pin from such a layer is rejected. An unspecified control means "host default" and is
never labeled with a level or model the run did not request.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import ExecutionCatalog, HostCapability, SupportTier

EXECUTION_SELECTION_SCHEMA_VERSION = 1


class SelectionLayer(enum.Enum):
    """The configuration layers a control can come from, highest precedence first."""

    RUN = "run"
    PROJECT = "project"
    USER = "user"
    WORKFLOW = "workflow"
    FACTORY = "factory"


PRECEDENCE: tuple[SelectionLayer, ...] = tuple(SelectionLayer)


class SelectionMode(enum.Enum):
    """How the selected controls were chosen."""

    MANUAL_PIN = "manual_pin"
    CONFIGURED_DEFAULT = "configured_default"
    HOST_DEFAULT = "host_default"


class HoldReason(enum.Enum):
    """Why a run may not progress until a human reconciles it."""

    CATALOG_DRIFT = "catalog_drift"
    HOST_FINGERPRINT_DRIFT = "host_fingerprint_drift"
    EFFECTIVE_CONTROL_UNVERIFIED = "effective_control_unverified"
    EFFECTIVE_CONTROL_MISMATCH = "effective_control_mismatch"


@dataclass(frozen=True, slots=True)
class ExecutionHold:
    """A fail-closed diagnostic: the run is held, its recorded binding is retained."""

    reason: HoldReason
    message: str
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise InvalidInputError("execution hold message must be non-blank")

    def to_state(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "message": self.message,
            "details": dict(self.details),
        }

    @classmethod
    def from_state(cls, state: object) -> ExecutionHold:
        if not isinstance(state, dict):
            raise InvalidInputError("execution hold state must be a mapping")
        details = state.get("details")
        message = state.get("message")
        if not isinstance(details, dict) or not isinstance(message, str):
            raise InvalidInputError("execution hold state is malformed")
        return cls(
            reason=_enum(HoldReason, state.get("reason"), "execution hold reason"),
            message=message,
            details={str(key): str(value) for key, value in details.items()},
        )


@dataclass(frozen=True, slots=True)
class RequestedControl:
    """One requested control: the value, the layer that supplied it, and whether it is a pin."""

    value: str | None
    source: SelectionLayer | None
    pinned: bool

    def __post_init__(self) -> None:
        if self.value is None:
            if self.source is not None or self.pinned:
                raise InvalidInputError("an unspecified control cannot carry a source or a pin")
            return
        if not self.value.strip():
            raise InvalidInputError("a requested control value must be non-blank")
        if self.source is None:
            raise InvalidInputError("a requested control value must name its source layer")

    def to_state(self) -> dict[str, object]:
        return {
            "value": self.value,
            "source": None if self.source is None else self.source.value,
            "pinned": self.pinned,
        }

    @classmethod
    def from_state(cls, state: object, *, name: str) -> RequestedControl:
        if not isinstance(state, dict):
            raise InvalidInputError(f"execution selection {name} must be a mapping")
        value = state.get("value")
        source = state.get("source")
        pinned = state.get("pinned")
        if (value is not None and not isinstance(value, str)) or not isinstance(pinned, bool):
            raise InvalidInputError(f"execution selection {name} is malformed")
        return cls(
            value=value,
            source=None if source is None else _enum(SelectionLayer, source, name),
            pinned=pinned,
        )


_UNSPECIFIED = RequestedControl(value=None, source=None, pinned=False)


@dataclass(frozen=True, slots=True)
class ExecutionSelection:
    """The bound, digest-identified execution selection of one run or task."""

    harness: str
    harness_source: SelectionLayer | None
    model: RequestedControl
    effort: RequestedControl
    selected_model: str | None
    selected_effort: str | None
    mode: SelectionMode
    strict_effective: bool
    catalog_version: str | None
    catalog_digest: Digest | None
    host_executable_version: str | None
    host_protocol_fingerprint: str | None
    support_tier: SupportTier | None
    legacy: bool = False

    def __post_init__(self) -> None:
        if not self.harness.strip():
            raise InvalidInputError("execution selection harness must be non-blank")
        for requested, selected, name in (
            (self.model, self.selected_model, "model"),
            (self.effort, self.selected_effort, "effort"),
        ):
            if requested.pinned and selected != requested.value:
                raise InvalidInputError(
                    f"a pinned {name} is exact and cannot be replaced by a different selection",
                    details={"requested": str(requested.value), "selected": str(selected)},
                )
            if selected != requested.value:
                raise InvalidInputError(f"selected {name} must equal the requested {name}")
        if self.legacy:
            self._require_legacy_shape()
            return
        if (
            self.harness_source is None
            or self.catalog_version is None
            or self.catalog_digest is None
            or self.host_executable_version is None
            or self.host_protocol_fingerprint is None
            or self.support_tier is None
        ):
            raise InvalidInputError(
                "a bound execution selection must name its harness source and catalog identity"
            )
        expected_mode = self._derived_mode()
        if self.mode is not expected_mode:
            raise InvalidInputError("execution selection mode does not match its controls")
        if self.strict_effective and not self.requires_native_controls:
            raise InvalidInputError(
                "a strict effective-control requirement needs at least one selected control"
            )

    @property
    def requires_native_controls(self) -> bool:
        """Whether any control must be honored natively by the host or rejected."""
        return self.selected_model is not None or self.selected_effort is not None

    @property
    def digest(self) -> Digest:
        """Return the digest that approvals and evidence bind to."""
        return Digest.of_json(self.to_state())

    @classmethod
    def legacy_host_default(cls, harness: str) -> ExecutionSelection:
        """Return the binding of a run persisted before selections existed.

        It requests nothing, names no catalog, and makes no claim about effective
        controls: legacy runs keep their original behavior and original fingerprint.
        """
        return cls(
            harness=harness,
            harness_source=None,
            model=_UNSPECIFIED,
            effort=_UNSPECIFIED,
            selected_model=None,
            selected_effort=None,
            mode=SelectionMode.HOST_DEFAULT,
            strict_effective=False,
            catalog_version=None,
            catalog_digest=None,
            host_executable_version=None,
            host_protocol_fingerprint=None,
            support_tier=None,
            legacy=True,
        )

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": EXECUTION_SELECTION_SCHEMA_VERSION,
            "harness": {
                "value": self.harness,
                "source": None if self.harness_source is None else self.harness_source.value,
            },
            "model": {**self.model.to_state(), "selected": self.selected_model},
            "effort": {**self.effort.to_state(), "selected": self.selected_effort},
            "mode": self.mode.value,
            "strict_effective": self.strict_effective,
            "catalog": {
                "version": self.catalog_version,
                "digest": None if self.catalog_digest is None else str(self.catalog_digest),
            },
            "host": {
                "executable_version": self.host_executable_version,
                "protocol_fingerprint": self.host_protocol_fingerprint,
                "support_tier": None if self.support_tier is None else self.support_tier.value,
            },
        }

    @classmethod
    def from_state(cls, state: object) -> ExecutionSelection:
        """Decode a persisted selection; an unknown schema version is rejected."""
        if not isinstance(state, dict):
            raise InvalidInputError("execution selection must be a mapping")
        version = state.get("schema_version")
        if version != EXECUTION_SELECTION_SCHEMA_VERSION:
            raise InvalidInputError(
                "execution selection schema version is unsupported",
                details={"schema_version": str(version)},
            )
        harness = _mapping(state, "harness")
        model = _mapping(state, "model")
        effort = _mapping(state, "effort")
        catalog = _mapping(state, "catalog")
        host = _mapping(state, "host")
        harness_value = harness.get("value")
        harness_source = harness.get("source")
        if not isinstance(harness_value, str) or not isinstance(harness_source, str):
            raise InvalidInputError("execution selection harness is malformed")
        digest_text = catalog.get("digest")
        if not isinstance(digest_text, str):
            raise InvalidInputError("execution selection catalog digest is malformed")
        algorithm, _, hex_value = digest_text.partition(":")
        return cls(
            harness=harness_value,
            harness_source=_enum(SelectionLayer, harness_source, "harness source"),
            model=RequestedControl.from_state(_strip_selected(model), name="model"),
            effort=RequestedControl.from_state(_strip_selected(effort), name="effort"),
            selected_model=_optional_text(model, "selected"),
            selected_effort=_optional_text(effort, "selected"),
            mode=_enum(SelectionMode, state.get("mode"), "mode"),
            strict_effective=_boolean(state, "strict_effective"),
            catalog_version=_required_text(catalog, "version"),
            catalog_digest=Digest(algorithm=algorithm, hex_value=hex_value),
            host_executable_version=_required_text(host, "executable_version"),
            host_protocol_fingerprint=_required_text(host, "protocol_fingerprint"),
            support_tier=_enum(SupportTier, host.get("support_tier"), "support tier"),
        )

    def _derived_mode(self) -> SelectionMode:
        if self.model.pinned or self.effort.pinned:
            return SelectionMode.MANUAL_PIN
        if self.requires_native_controls:
            return SelectionMode.CONFIGURED_DEFAULT
        return SelectionMode.HOST_DEFAULT

    def _require_legacy_shape(self) -> None:
        if (
            self.requires_native_controls
            or self.mode is not SelectionMode.HOST_DEFAULT
            or self.strict_effective
            or self.catalog_digest is not None
            or self.support_tier is not None
        ):
            raise InvalidInputError("a legacy execution selection is a bare host default")


@dataclass(frozen=True, slots=True)
class LayerControls:
    """The explicit execution inputs one configuration layer contributes.

    Layers are typed resolver inputs; nothing here reads a file. ``model_pinned`` and
    ``effort_pinned`` mark values the operator pinned manually. The ``allowed_*``
    fields are hard policy constraints: the resolved value must satisfy the constraints
    of every layer, and a constrained control that was left unspecified is rejected
    because the host's default cannot be proven to satisfy it.
    """

    layer: SelectionLayer
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    model_pinned: bool = False
    effort_pinned: bool = False
    strict_effective: bool | None = None
    allowed_harnesses: tuple[str, ...] | None = None
    allowed_models: tuple[str, ...] | None = None
    allowed_efforts: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.harness, "harness"),
            (self.model, "model"),
            (self.effort, "effort"),
        ):
            if value is not None and not value.strip():
                raise InvalidInputError(f"{self.layer.value} layer {name} must be non-blank")
        if self.model_pinned and self.model is None:
            raise InvalidInputError(f"{self.layer.value} layer pins a model it does not specify")
        if self.effort_pinned and self.effort is None:
            raise InvalidInputError(f"{self.layer.value} layer pins an effort it does not specify")


def resolve_execution_selection(
    layers: Sequence[LayerControls], *, catalog: ExecutionCatalog
) -> ExecutionSelection:
    """Resolve explicit layer inputs into one validated, catalog-bound selection.

    Never substitutes, aliases, or falls back: an unsupported harness, model form, or
    effort level is rejected with an actionable error, never repaired.
    """
    seen: set[SelectionLayer] = set()
    for layer in layers:
        if layer.layer in seen:
            raise InvalidInputError(
                f"{layer.layer.value} layer supplied more than once",
                details={"layer": layer.layer.value},
            )
        seen.add(layer.layer)
    ordered = sorted(layers, key=lambda item: PRECEDENCE.index(item.layer))

    harness = next(((item.harness, item.layer) for item in ordered if item.harness), None)
    if harness is None:
        raise InvalidInputError("no layer selected a harness")
    harness_name, harness_layer = harness
    host = catalog.host(harness_name)
    host.require_usable()

    # Model and effort vocabularies are host-specific, so a layer that names another
    # harness contributes neither. Quietly dropping its pin would break the pin, so a
    # pin from such a layer is rejected instead.
    eligible = [item for item in ordered if item.harness in (None, harness_name)]
    for item in ordered:
        if item in eligible:
            continue
        for pinned, name in ((item.model_pinned, "model"), (item.effort_pinned, "effort")):
            if pinned:
                raise InvalidInputError(
                    f"the {item.layer.value} layer pins the {name} for harness "
                    f"{item.harness!r}, but this run resolves harness {harness_name!r}; "
                    "model and effort pins are host-specific and are never carried across hosts",
                    details={
                        "layer": item.layer.value,
                        "control": name,
                        "layer_harness": str(item.harness),
                        "resolved_harness": harness_name,
                    },
                )

    model = _pick(eligible, "model")
    effort = _pick(eligible, "effort")
    if model.value is not None:
        host.require_model_pin(model.value)
    if effort.value is not None:
        host.require_effort_level(effort.value)

    for item in ordered:
        _enforce_constraints(
            item,
            harness=harness_name,
            model=model.value,
            effort=effort.value,
            applies_to_harness=item in eligible,
        )

    strict = any(item.strict_effective is True for item in eligible)
    pinned = model.pinned or effort.pinned
    mode = (
        SelectionMode.MANUAL_PIN
        if pinned
        else SelectionMode.CONFIGURED_DEFAULT
        if model.value is not None or effort.value is not None
        else SelectionMode.HOST_DEFAULT
    )
    return ExecutionSelection(
        harness=harness_name,
        harness_source=harness_layer,
        model=model,
        effort=effort,
        selected_model=model.value,
        selected_effort=effort.value,
        mode=mode,
        strict_effective=strict,
        catalog_version=catalog.version,
        catalog_digest=catalog.digest,
        host_executable_version=host.executable_version,
        host_protocol_fingerprint=host.protocol_fingerprint,
        support_tier=host.support_tier,
    )


def detect_catalog_drift(
    selection: ExecutionSelection, *, current: ExecutionCatalog
) -> ExecutionHold | None:
    """Return a hold when a bound selection's catalog is no longer the current one.

    Legacy bindings name no catalog and are never drifted. A held run keeps its original
    binding; nothing is rebound to the current catalog.
    """
    if selection.legacy or selection.catalog_digest == current.digest:
        return None
    return ExecutionHold(
        reason=HoldReason.CATALOG_DRIFT,
        message=(
            "the execution catalog changed since this run was bound; the run is held and its "
            "original binding is retained. Restore the catalog it was bound to, or start a "
            "new run from a rebuilt request"
        ),
        details={
            "bound_catalog_version": str(selection.catalog_version),
            "bound_catalog_digest": str(selection.catalog_digest),
            "current_catalog_version": current.version,
            "current_catalog_digest": str(current.digest),
        },
    )


def validate_against_catalog(
    selection: ExecutionSelection, *, catalog: ExecutionCatalog
) -> HostCapability:
    """Reject a bound selection that is not exactly what the catalog supports.

    The selection must name this catalog's version and digest, its host's tested
    fingerprint and tier, and every selected control must still be an exact pin in the
    host's own vocabulary. Nothing is repaired or defaulted, so a hand-edited request
    cannot smuggle in an alias, an unpinnable level, a higher tier or another version.
    """
    if selection.legacy:
        raise InvalidInputError("a legacy execution selection names no catalog to validate")
    if selection.catalog_digest != catalog.digest or selection.catalog_version != catalog.version:
        raise InvalidInputError(
            "the execution selection was bound to a different catalog than the one in use",
            details={
                "bound_catalog_digest": str(selection.catalog_digest),
                "current_catalog_digest": str(catalog.digest),
            },
        )
    host = catalog.host(selection.harness)
    host.require_usable()
    if (
        selection.host_executable_version != host.executable_version
        or selection.host_protocol_fingerprint != host.protocol_fingerprint
        or selection.support_tier is not host.support_tier
    ):
        raise InvalidInputError(
            "the execution selection does not match the catalog's tested host entry",
            details={"harness": selection.harness},
        )
    if selection.selected_model is not None:
        host.require_model_pin(selection.selected_model)
    if selection.selected_effort is not None:
        host.require_effort_level(selection.selected_effort)
    return host


def _pick(eligible: Sequence[LayerControls], control: str) -> RequestedControl:
    """Pick one control by plain layer precedence; a pin only marks it exact.

    The highest-precedence layer that specifies the control wins. A lower layer that pins
    a different value is a conflict and is rejected, never resolved silently in either
    direction. A lower pin equal to the winner confirms it and makes it exact.
    """
    candidates: list[tuple[SelectionLayer, str, bool]] = []
    for item in eligible:
        value = item.model if control == "model" else item.effort
        pinned = item.model_pinned if control == "model" else item.effort_pinned
        if value is not None:
            candidates.append((item.layer, value, pinned))
    if not candidates:
        return _UNSPECIFIED
    layer, value, pinned = candidates[0]
    for lower_layer, lower_value, lower_pinned in candidates[1:]:
        if not lower_pinned:
            continue
        if lower_value != value:
            raise InvalidInputError(
                f"the {lower_layer.value} layer pins {control} {lower_value!r}, but the "
                f"{layer.value} layer sets {value!r}; a pin is never overridden or chosen "
                "between silently, so remove one of them",
                details={
                    "control": control,
                    "pinned_layer": lower_layer.value,
                    "pinned_value": lower_value,
                    "higher_layer": layer.value,
                    "higher_value": value,
                },
            )
        pinned = True
    return RequestedControl(value=value, source=layer, pinned=pinned)


def _enforce_constraints(
    item: LayerControls,
    *,
    harness: str,
    model: str | None,
    effort: str | None,
    applies_to_harness: bool,
) -> None:
    # A layer that names a different harness constrains that harness's models and
    # efforts, not this one's; only the harness constraint is universal.
    for allowed, value, name in (
        (item.allowed_harnesses, harness, "harness"),
        (item.allowed_models if applies_to_harness else None, model, "model"),
        (item.allowed_efforts if applies_to_harness else None, effort, "effort"),
    ):
        if allowed is None:
            continue
        if value is None:
            raise InvalidInputError(
                f"the {item.layer.value} layer constrains {name} to {', '.join(allowed)}; "
                f"an explicit {name} is required because the host default cannot be proven "
                "to satisfy it",
                details={"layer": item.layer.value, "control": name},
            )
        if value not in allowed:
            raise InvalidInputError(
                f"{name} {value!r} violates the {item.layer.value} layer constraint "
                f"({', '.join(allowed)})",
                details={"layer": item.layer.value, "control": name, "value": value},
            )


def _strip_selected(state: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in state.items() if key != "selected"}


def _mapping(state: Mapping[str, object], key: str) -> dict[str, object]:
    value = state.get(key)
    if not isinstance(value, dict):
        raise InvalidInputError(f"execution selection {key} must be a mapping")
    return value


def _optional_text(state: Mapping[str, object], key: str) -> str | None:
    value = state.get(key)
    if value is not None and not isinstance(value, str):
        raise InvalidInputError(f"execution selection {key} must be a string or null")
    return value


def _required_text(state: Mapping[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"execution selection {key} must be a non-blank string")
    return value


def _boolean(state: Mapping[str, object], key: str) -> bool:
    value = state.get(key)
    if not isinstance(value, bool):
        raise InvalidInputError(f"execution selection {key} must be a boolean")
    return value


def _enum[EnumT: enum.Enum](kind: type[EnumT], value: object, name: str) -> EnumT:
    try:
        return kind(value)
    except ValueError as error:
        raise InvalidInputError(f"execution selection {name} is unknown") from error


__all__ = [
    "EXECUTION_SELECTION_SCHEMA_VERSION",
    "PRECEDENCE",
    "ExecutionHold",
    "ExecutionSelection",
    "HoldReason",
    "LayerControls",
    "RequestedControl",
    "SelectionLayer",
    "SelectionMode",
    "detect_catalog_drift",
    "resolve_execution_selection",
    "validate_against_catalog",
]
