"""Compile the five configuration layers into one immutable effective configuration.

Precedence is run > project > user > workflow > factory. Harness, model and effort are
resolved by :func:`enginery.domain.execution_selection.resolve_execution_selection`, which
also applies every layer's hard ``allowed_*`` constraints; this module feeds it and never
duplicates its selection logic. Every other field is "highest layer that specifies it
wins" (protected paths are additive across layers and never remove a built-in), and the
hard ``max_*``/``allowed_*`` policy of every layer, including the run layer's overrides,
is enforced before anything is built.

The result, :class:`EffectiveConfig`, records for every compiled field its value, the
winning layer and every shadowed lower-layer value with its source file and sha256. Its
canonical JSON is the configuration snapshot: the sha256 of that JSON is the request's
``configuration_snapshot_digest``. ``explain``, ``doctor`` and ``build-request`` all
consume this one compiler output; there is no second resolution path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.layered_config import (
    DEFAULT_PROFILE,
    FIELD_NAMES,
    LAYER_SCHEMA_VERSION,
    RUNNER_MODES,
    ConfigError,
    LayerDocument,
    Value,
    load_factory_layer,
    load_workflow_layer,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InternalInvariantViolationError, InvalidInputError
from enginery.domain.execution_catalog import ExecutionCatalog, InstalledHost
from enginery.domain.execution_selection import (
    PRECEDENCE,
    ExecutionSelection,
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.runner import MODEL_ACCESS_NONE, RunnerProfile, RunnerSelection

MERGED = "merged"
UNSET = "unset"
_PROFILE_DEFAULT_SOURCE = "built-in default profile"


COMPILED_FIELD_NAMES = tuple(
    name for name in FIELD_NAMES if name not in {"model_pinned", "effort_pinned"}
)
"""Every compiled field: a pin is part of its model or effort field, not a field of its own."""
type FrozenValue = str | bool | int | None | tuple[FrozenValue, ...]
type HostProbe = Callable[[str, str], InstalledHost | None]


def _freeze(value: object) -> FrozenValue:
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    raise InvalidInputError("configuration values must be JSON scalars or lists")


def _thaw(value: FrozenValue) -> object:
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ChainEntry:
    """One layer's statement of a field: its value and exactly where it was written."""

    layer: str
    value: FrozenValue
    source: str
    source_digest: str | None
    pinned: bool = False
    note: str | None = None

    def to_state(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "value": _thaw(self.value),
            "source": self.source,
            "source_digest": self.source_digest,
            "pinned": self.pinned,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CompiledField:
    """One compiled field: its value, the winning layer and the full source chain."""

    name: str
    value: FrozenValue
    winner: str
    chain: tuple[ChainEntry, ...]

    @property
    def shadowed(self) -> tuple[ChainEntry, ...]:
        """The lower-precedence statements the winner overrides."""
        return self.chain[1:] if self.winner not in {MERGED, UNSET} else ()

    def to_state(self) -> dict[str, object]:
        return {
            "value": _thaw(self.value),
            "winner": self.winner,
            "chain": [entry.to_state() for entry in self.chain],
        }


@dataclass(frozen=True, slots=True)
class AppliedConstraint:
    """A hard constraint some layer states; every one binds every layer."""

    layer: str
    source: str
    name: str
    value: FrozenValue

    def to_state(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "source": self.source,
            "name": self.name,
            "value": _thaw(self.value),
        }


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One layer document that contributed: its path and the sha256 of its bytes."""

    layer: str
    source: str
    digest: str | None

    def to_state(self) -> dict[str, object]:
        return {"layer": self.layer, "source": self.source, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """The compiled, immutable configuration a Stage 1 request is built from.

    ``to_state`` is the canonical configuration snapshot and ``digest`` its sha256.
    """

    fields: tuple[CompiledField, ...]
    constraints: tuple[AppliedConstraint, ...]
    sources: tuple[SourceRecord, ...]
    catalog_version: str
    catalog_digest: str
    selection_digest: str
    schema_version: int = LAYER_SCHEMA_VERSION

    def field(self, name: str) -> CompiledField:
        for item in self.fields:
            if item.name == name:
                return item
        raise InvalidInputError(f"no compiled configuration field {name!r}")

    def value(self, name: str) -> FrozenValue:
        return self.field(name).value

    def plain_value(self, name: str) -> object:
        """The value as plain JSON data (lists, not tuples), for comparison and display."""
        return _thaw(self.field(name).value)

    def text(self, name: str) -> str:
        value = self.value(name)
        if not isinstance(value, str):
            raise InternalInvariantViolationError(f"compiled field {name} is not text")
        return value

    def strings(self, name: str) -> tuple[str, ...]:
        value = self.value(name)
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            raise InternalInvariantViolationError(f"compiled field {name} is not a list of text")
        return tuple(str(item) for item in value)

    def integer(self, name: str) -> int:
        value = self.value(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise InternalInvariantViolationError(f"compiled field {name} is not an integer")
        return value

    def commands(self) -> tuple[tuple[str, ...], ...]:
        value = self.value("validation_commands")
        if not isinstance(value, tuple):
            raise InternalInvariantViolationError("compiled validation_commands is not a list")
        return tuple(
            tuple(str(argument) for argument in command)
            for command in value
            if isinstance(command, tuple)
        )

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog": {"version": self.catalog_version, "digest": self.catalog_digest},
            "selection_digest": self.selection_digest,
            "sources": [record.to_state() for record in self.sources],
            "constraints": [constraint.to_state() for constraint in self.constraints],
            "fields": {item.name: item.to_state() for item in self.fields},
        }

    @property
    def digest(self) -> Digest:
        return Digest.of_json(self.to_state())

    @classmethod
    def from_state(cls, state: object) -> EffectiveConfig:
        """Decode a snapshot strictly; anything not exactly the compiler's shape is refused."""
        mapping = _mapping(state, "configuration snapshot")
        _exact_keys(
            mapping,
            {"schema_version", "catalog", "selection_digest", "sources", "constraints", "fields"},
            "configuration snapshot",
        )
        if mapping["schema_version"] != LAYER_SCHEMA_VERSION:
            raise InvalidInputError(
                "unsupported configuration snapshot schema; rebuild the request with the "
                "current `enginery stage1 build-request`",
                details={"schema_version": str(mapping["schema_version"])},
            )
        catalog = _mapping(mapping["catalog"], "snapshot catalog")
        _exact_keys(catalog, {"version", "digest"}, "snapshot catalog")
        raw_fields = _mapping(mapping["fields"], "snapshot fields")
        if set(raw_fields) != set(COMPILED_FIELD_NAMES):
            raise InvalidInputError(
                "configuration snapshot does not carry exactly the compiled fields",
                details={"missing": sorted(set(COMPILED_FIELD_NAMES) - set(raw_fields))},
            )
        fields = tuple(_field_from_state(name, raw_fields[name]) for name in COMPILED_FIELD_NAMES)
        constraints = tuple(
            _constraint_from_state(item)
            for item in _sequence(mapping["constraints"], "constraints")
        )
        sources = tuple(
            _source_from_state(item) for item in _sequence(mapping["sources"], "sources")
        )
        return cls(
            fields=fields,
            constraints=constraints,
            sources=sources,
            catalog_version=_string(catalog["version"], "catalog version"),
            catalog_digest=_string(catalog["digest"], "catalog digest"),
            selection_digest=_string(mapping["selection_digest"], "selection digest"),
        )


@dataclass(frozen=True, slots=True)
class CompiledConfig:
    """The compiler output: the snapshot plus the selection and runner it resolved."""

    config: EffectiveConfig
    selection: ExecutionSelection
    runner: RunnerSelection


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidInputError(f"{what} must be a mapping")
    return value


def _sequence(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidInputError(f"{what} must be a list")
    return value


def _string(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidInputError(f"{what} must be a string")
    return value


def _exact_keys(mapping: Mapping[str, object], keys: set[str], what: str) -> None:
    if set(mapping) != keys:
        raise InvalidInputError(
            f"{what} does not have the expected keys", details={"expected": sorted(keys)}
        )


def _entry_from_state(state: object) -> ChainEntry:
    mapping = _mapping(state, "snapshot chain entry")
    _exact_keys(
        mapping, {"layer", "value", "source", "source_digest", "pinned", "note"}, "chain entry"
    )
    digest = mapping["source_digest"]
    note = mapping["note"]
    pinned = mapping["pinned"]
    if (digest is not None and not isinstance(digest, str)) or not isinstance(pinned, bool):
        raise InvalidInputError("snapshot chain entry is malformed")
    if note is not None and not isinstance(note, str):
        raise InvalidInputError("snapshot chain entry note is malformed")
    return ChainEntry(
        layer=_string(mapping["layer"], "chain layer"),
        value=_freeze(mapping["value"]),
        source=_string(mapping["source"], "chain source"),
        source_digest=digest,
        pinned=pinned,
        note=note,
    )


def _field_from_state(name: str, state: object) -> CompiledField:
    mapping = _mapping(state, f"snapshot field {name}")
    _exact_keys(mapping, {"value", "winner", "chain"}, f"snapshot field {name}")
    return CompiledField(
        name=name,
        value=_freeze(mapping["value"]),
        winner=_string(mapping["winner"], "field winner"),
        chain=tuple(_entry_from_state(item) for item in _sequence(mapping["chain"], "chain")),
    )


def _constraint_from_state(state: object) -> AppliedConstraint:
    mapping = _mapping(state, "snapshot constraint")
    _exact_keys(mapping, {"layer", "source", "name", "value"}, "snapshot constraint")
    return AppliedConstraint(
        layer=_string(mapping["layer"], "constraint layer"),
        source=_string(mapping["source"], "constraint source"),
        name=_string(mapping["name"], "constraint name"),
        value=_freeze(mapping["value"]),
    )


def _source_from_state(state: object) -> SourceRecord:
    mapping = _mapping(state, "snapshot source")
    _exact_keys(mapping, {"layer", "source", "digest"}, "snapshot source")
    digest = mapping["digest"]
    if digest is not None and not isinstance(digest, str):
        raise InvalidInputError("snapshot source digest is malformed")
    return SourceRecord(
        layer=_string(mapping["layer"], "source layer"),
        source=_string(mapping["source"], "source source"),
        digest=digest,
    )


def _entry(
    document: LayerDocument, name: str, value: Value, *, note: str | None = None
) -> ChainEntry:
    pinned = False
    if name in {"model", "effort"}:
        pinned = bool(document.values.get(f"{name}_pinned", False))
    return ChainEntry(
        layer=document.layer.value,
        value=_freeze(value),
        source=document.source,
        source_digest=None if document.digest is None else str(document.digest),
        pinned=pinned,
        note=note,
    )


def _chain(
    documents: Sequence[LayerDocument],
    name: str,
    *,
    ineligible: Callable[[LayerDocument], bool] | None = None,
) -> tuple[ChainEntry, ...]:
    return tuple(
        _entry(
            document,
            name,
            document.values[name],
            note="names a different harness; contributes nothing to the resolved harness"
            if ineligible is not None and ineligible(document)
            else None,
        )
        for document in documents
        if name in document.values
    )


def _select_profile(
    supplied: Mapping[SelectionLayer, LayerDocument | None],
) -> tuple[str, LayerDocument | None, tuple[ChainEntry, ...]]:
    chain: list[ChainEntry] = []
    selector: LayerDocument | None = None
    name = DEFAULT_PROFILE
    for layer in (SelectionLayer.RUN, SelectionLayer.PROJECT, SelectionLayer.USER):
        document = supplied.get(layer)
        if document is None or "profile" not in document.values:
            continue
        value = document.values["profile"]
        assert isinstance(value, str)
        if selector is None:
            selector, name = document, value
        chain.append(_entry(document, "profile", value))
    chain.append(
        ChainEntry(
            layer=SelectionLayer.FACTORY.value,
            value=DEFAULT_PROFILE,
            source=_PROFILE_DEFAULT_SOURCE,
            source_digest=None,
        )
    )
    return name, selector, tuple(chain)


def _controls(document: LayerDocument) -> LayerControls:
    values = document.values
    for control in ("model", "effort"):
        if values.get(f"{control}_pinned") and control not in values:
            raise ConfigError(
                f"pins a {control} that this layer does not specify",
                file=document.source,
                key=f"execution.{control}_pinned",
            )

    def text(name: str) -> str | None:
        value = values.get(name)
        return value if isinstance(value, str) else None

    def allowed(name: str) -> tuple[str, ...] | None:
        value = document.constraints.get(name)
        return tuple(value) if isinstance(value, list) else None  # type: ignore[arg-type]

    strict = values.get("strict_effective")
    return LayerControls(
        layer=document.layer,
        harness=text("harness"),
        model=text("model"),
        effort=text("effort"),
        model_pinned=bool(values.get("model_pinned", False)),
        effort_pinned=bool(values.get("effort_pinned", False)),
        strict_effective=strict if isinstance(strict, bool) else None,
        allowed_harnesses=allowed("allowed_harnesses"),
        allowed_models=allowed("allowed_models"),
        allowed_efforts=allowed("allowed_efforts"),
    )


_CONTROL_KEYS = {
    "harness": "execution.harness",
    "model": "execution.model",
    "effort": "execution.effort",
    "strict_effective": "execution.strict_effective",
}


def _contextualize(
    error: InvalidInputError,
    documents: Sequence[LayerDocument],
    winners: Mapping[str, LayerDocument | None],
) -> ConfigError:
    """Carry a resolver refusal to the file and key that caused it."""
    by_layer = {document.layer.value: document for document in documents}
    layer = error.details.get("layer")
    control = error.details.get("control")
    document = by_layer.get(str(layer)) if layer is not None else None
    key = _CONTROL_KEYS.get(str(control), "execution")
    if document is None:
        message = str(error)
        for name in ("model", "effort", "harness"):
            winner = winners.get(name)
            value = None if winner is None else winner.values.get(name)
            if winner is not None and isinstance(value, str) and repr(value) in message:
                document, key = winner, _CONTROL_KEYS[name]
                break
    file = document.source if document is not None else ", ".join(d.source for d in documents)
    wrapped = ConfigError(str(error), file=file, key=key, details=dict(error.details))
    return wrapped


def _first(documents: Sequence[LayerDocument], name: str) -> LayerDocument | None:
    return next((document for document in documents if name in document.values), None)


def compile_effective_config(
    *,
    project: LayerDocument | None,
    user: LayerDocument | None,
    run: LayerDocument | None,
    catalog: ExecutionCatalog | None = None,
    probe_host: HostProbe | None = None,
) -> CompiledConfig:
    """Resolve the layers into one effective configuration, or refuse with file/key context.

    ``probe_host(harness, executable)`` observes the installed host only when a model or
    effort is requested and the run is trusted-local; an isolated run never consults the
    operator's own binary.
    """
    supplied = {SelectionLayer.RUN: run, SelectionLayer.PROJECT: project, SelectionLayer.USER: user}
    profile_name, selector, profile_chain = _select_profile(supplied)
    factory = load_factory_layer()
    workflow = load_workflow_layer(profile_name, selected_by=selector)
    documents = sorted(
        [
            *(document for document in supplied.values() if document is not None),
            workflow,
            factory,
        ],
        key=lambda document: PRECEDENCE.index(document.layer),
    )
    catalog = load_execution_catalog() if catalog is None else catalog

    def winner_of(name: str) -> LayerDocument | None:
        return _first(documents, name)

    def value_of(name: str) -> Value | None:
        winner = winner_of(name)
        return None if winner is None else winner.values[name]

    # Runner first: it decides whether the host binary may be probed.
    mode_winner = winner_of("runner_mode")
    if mode_winner is None:
        raise ConfigError(
            "no layer declares a trust mode; pass --runner or set it in project policy",
            file="<all layers>",
            key="runner.mode",
            allowed=RUNNER_MODES,
        )
    mode = mode_winner.values["runner_mode"]
    assert isinstance(mode, str)
    runner = _runner(mode, value_of, winner_of)

    executable_override = value_of("harness_executable")

    def executable_for(harness: str) -> str:
        if isinstance(executable_override, str):
            return executable_override
        try:
            return factory.provider_executables[harness]
        except KeyError as error:
            raise InvalidInputError(
                "Stage 1 harness_provider is not a configured harness",
                details={"harness": harness},
            ) from error

    resolver_probe = (
        None
        if runner.isolated_mode or probe_host is None
        else lambda harness: probe_host(harness, executable_for(harness))
    )
    controls = [_controls(document) for document in documents]
    try:
        selection = resolve_execution_selection(
            controls, catalog=catalog, probe_host=resolver_probe
        )
    except ConfigError:
        raise
    except InvalidInputError as error:
        raise _contextualize(
            error, documents, {name: winner_of(name) for name in ("model", "effort", "harness")}
        ) from error

    values: dict[str, FrozenValue] = {}
    fields: dict[str, CompiledField] = {}

    def record(name: str, value: object, winner: str, chain: tuple[ChainEntry, ...]) -> None:
        values[name] = _freeze(value)
        fields[name] = CompiledField(name=name, value=_freeze(value), winner=winner, chain=chain)

    def generic(name: str, *, fallback: object = None) -> None:
        chain = _chain(documents, name)
        if chain:
            record(name, chain[0].value, chain[0].layer, chain)
        else:
            record(name, fallback, UNSET, ())

    harness = selection.harness

    def ineligible(document: LayerDocument) -> bool:
        layer_harness = document.values.get("harness")
        return layer_harness is not None and layer_harness != harness

    record(
        "profile",
        profile_name,
        profile_chain[0].layer,
        profile_chain,
    )
    record(
        "harness",
        harness,
        "" if selection.harness_source is None else selection.harness_source.value,
        _chain(documents, "harness"),
    )
    for name, requested in (("model", selection.model), ("effort", selection.effort)):
        chain = _chain(documents, name, ineligible=ineligible)
        record(
            name,
            requested.value,
            UNSET if requested.source is None else requested.source.value,
            tuple(
                sorted(
                    chain,
                    key=lambda item: (
                        item.note is not None,
                        PRECEDENCE.index(SelectionLayer(item.layer)),
                    ),
                )
            ),
        )
    strict_chain = _chain(documents, "strict_effective", ineligible=ineligible)
    strict_winner = next(
        (entry for entry in strict_chain if entry.value is selection.strict_effective), None
    )
    record(
        "strict_effective",
        selection.strict_effective,
        UNSET if strict_winner is None else strict_winner.layer,
        strict_chain,
    )
    record("runner_mode", mode, mode_winner.layer.value, _chain(documents, "runner_mode"))
    for name in ("runner_image", "docker_context"):
        generic(name)
    generic("model_access", fallback=MODEL_ACCESS_NONE)
    generic("validation_commands")
    for name in (
        "required_checks",
        "repair_limit",
        "time_budget_seconds",
        "permitted_capabilities",
        "evidence_requirements",
        "github_credential_reference",
        "harness_credential_reference",
        "github_executable",
    ):
        generic(name)
    chain = _chain(documents, "harness_executable")
    if chain:
        record("harness_executable", chain[0].value, chain[0].layer, chain)
    else:
        record(
            "harness_executable",
            executable_for(harness),
            SelectionLayer.FACTORY.value,
            (
                ChainEntry(
                    layer=SelectionLayer.FACTORY.value,
                    value=executable_for(harness),
                    source=factory.source,
                    source_digest=None if factory.digest is None else str(factory.digest),
                    note=f"default executable of harness {harness}",
                ),
            ),
        )
    protected_chain = _chain(documents, "protected_paths")
    merged: list[str] = []
    for entry in protected_chain:
        assert isinstance(entry.value, tuple)
        merged.extend(item for item in entry.value if isinstance(item, str) and item not in merged)
    record("protected_paths", merged, MERGED, protected_chain)

    _require_validation_for_isolation(mode, fields["validation_commands"])
    constraints = _enforce_constraints(documents, fields)
    config = EffectiveConfig(
        fields=tuple(fields[name] for name in COMPILED_FIELD_NAMES),
        constraints=constraints,
        sources=tuple(
            SourceRecord(
                layer=document.layer.value,
                source=document.source,
                digest=None if document.digest is None else str(document.digest),
            )
            for document in documents
        ),
        catalog_version=catalog.version,
        catalog_digest=str(catalog.digest),
        selection_digest=str(selection.digest),
    )
    return CompiledConfig(config=config, selection=selection, runner=runner)


def _runner(
    mode: str,
    value_of: Callable[[str], Value | None],
    winner_of: Callable[[str], LayerDocument | None],
) -> RunnerSelection:
    image = value_of("runner_image")
    context = value_of("docker_context")
    access = value_of("model_access")
    access = MODEL_ACCESS_NONE if access is None else access
    if mode == "trusted-local":
        for name, key, present in (
            ("runner_image", "runner.image", image is not None),
            ("docker_context", "runner.docker_context", context is not None),
            ("model_access", "runner.model_access", access != MODEL_ACCESS_NONE),
        ):
            if present:
                document = winner_of(name)
                raise ConfigError(
                    "applies only to the isolated-oci trust mode, but the run resolves "
                    "trusted-local; remove it or choose --runner isolated-oci",
                    file="<all layers>" if document is None else document.source,
                    key=key,
                )
        return RunnerSelection.trusted_local()
    if image is None:
        raise ConfigError(
            "the isolated-oci trust mode requires an exact pinned image "
            "(--runner-image sha256:<image id>; build one with `enginery runner build-image`)",
            file="<all layers>",
            key="runner.image",
        )
    assert isinstance(image, str)
    assert isinstance(context, str | None) and isinstance(access, str)
    try:
        return RunnerSelection.isolated(
            image=image, profile=RunnerProfile(), docker_context=context, model_access=access
        )
    except InvalidInputError as error:
        document = winner_of("runner_image")
        raise ConfigError(
            str(error),
            file="<all layers>" if document is None else document.source,
            key="runner.image",
            details=dict(error.details),
        ) from error


def _require_validation_for_isolation(mode: str, validation: CompiledField) -> None:
    if mode == "isolated-oci" and validation.winner == SelectionLayer.FACTORY.value:
        raise InvalidInputError(
            "an isolated run requires --validation-command (or [validation] commands in project "
            "policy): isolated validation runs offline in the pinned runner image, which has "
            "python3, node, git, uv and sh but not the project's test dependencies, so there is "
            "no default that can run there"
        )


def _enforce_constraints(
    documents: Sequence[LayerDocument], fields: Mapping[str, CompiledField]
) -> tuple[AppliedConstraint, ...]:
    """Apply every layer's hard policy to the compiled values and record what applied.

    Harness, model and effort constraints were already enforced by the resolver.
    """
    applied: list[AppliedConstraint] = []
    for document in documents:
        for name, value in document.constraints.items():
            applied.append(
                AppliedConstraint(
                    layer=document.layer.value,
                    source=document.source,
                    name=name,
                    value=_freeze(value),
                )
            )
            _check_constraint(document, name, value, fields)
    return tuple(applied)


def _violation(
    document: LayerDocument, name: str, subject: CompiledField, value: object, rule: str
) -> ConfigError:
    origin = subject.chain[0].source if subject.chain else "no layer"
    return ConfigError(
        f"{subject.name} {value!r} (from the {subject.winner} layer, {origin}) violates this "
        f"hard constraint: {rule}",
        file=document.source,
        key=f"constraints.{name}",
        details={"layer": document.layer.value, "field": subject.name},
    )


def _check_constraint(
    document: LayerDocument, name: str, value: Value, fields: Mapping[str, CompiledField]
) -> None:
    if name == "allowed_runner_modes":
        subject = fields["runner_mode"]
        assert isinstance(value, list)
        if subject.value not in value:
            raise _violation(
                document, name, subject, subject.value, f"one of {', '.join(map(str, value))}"
            )
    elif name == "allowed_capabilities":
        subject = fields["permitted_capabilities"]
        assert isinstance(value, list) and isinstance(subject.value, tuple)
        outside = [item for item in subject.value if item not in value]
        if outside:
            raise _violation(
                document,
                name,
                subject,
                outside[0],
                f"permitted capabilities must be among {', '.join(map(str, value)) or 'none'}",
            )
    elif name in {"max_time_budget_seconds", "max_repair_limit"}:
        subject = fields[
            "time_budget_seconds" if name == "max_time_budget_seconds" else "repair_limit"
        ]
        assert isinstance(value, int) and isinstance(subject.value, int)
        if subject.value > value:
            raise _violation(document, name, subject, subject.value, f"at most {value}")


__all__ = [
    "MERGED",
    "UNSET",
    "AppliedConstraint",
    "ChainEntry",
    "CompiledConfig",
    "CompiledField",
    "EffectiveConfig",
    "HostProbe",
    "SourceRecord",
    "compile_effective_config",
]
