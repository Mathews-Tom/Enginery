"""Strict layered-configuration documents: the typed schema of every configuration layer.

Five layers feed one Stage 1 request: run (command-line flags), project (the tracked
``.enginery/project.toml``), user (the private ``user.toml``), workflow (a packaged,
versioned profile) and factory (packaged defaults). Every layer is the same typed
document; a layer file is parsed strictly and never coerced: an unknown key or section,
a wrong type, an unsupported schema version, a credential-shaped key or value (project
policy only) or an unknown workflow profile is refused with a :class:`ConfigError` that
names the file, the key and, where the vocabulary is closed, the allowed values.

This module is pure: it reads no file but the packaged layers. Discovery and file input
belong to the CLI; precedence and compilation live in
:mod:`enginery.application.effective_config`.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import SelectionLayer
from enginery.domain.runner import MODEL_ACCESS_MODES

LAYER_SCHEMA_VERSION = 1
RUNNER_MODES: tuple[str, ...] = ("trusted-local", "isolated-oci")
FACTORY_DEFAULTS_FILE = "factory-defaults-v1.toml"
WORKFLOW_PROFILES: dict[str, str] = {
    "standard": "workflow-profile-standard-v1.toml",
    "careful": "workflow-profile-careful-v1.toml",
}
DEFAULT_PROFILE = "standard"
RUN_SOURCE = "command line"

_CONFIG_DIRECTORY = Path(__file__).parent.parent / "config"

type Value = str | bool | int | list[str] | list[list[str]]


class ConfigError(InvalidInputError):
    """A configuration layer is invalid; names the file, the key and the allowed values."""

    def __init__(
        self,
        message: str,
        *,
        file: str,
        key: str,
        allowed: tuple[str, ...] = (),
        details: Mapping[str, object] | None = None,
    ) -> None:
        suffix = f" (allowed: {', '.join(allowed)})" if allowed else ""
        super().__init__(
            f"{file}: {key}: {message}{suffix}",
            details={
                "file": file,
                "key": key,
                "allowed": list(allowed),
                **(details or {}),
            },
        )
        self.file = file
        self.key = key
        self.allowed = allowed


@dataclass(frozen=True, slots=True)
class _Spec:
    """One configurable field: where it lives in TOML, its flat name and its value kind."""

    section: str | None
    key: str
    name: str
    kind: str
    allowed: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.key if self.section is None else f"{self.section}.{self.key}"


FIELD_SPECS: tuple[_Spec, ...] = (
    _Spec(None, "profile", "profile", "str"),
    _Spec("execution", "harness", "harness", "str"),
    _Spec("execution", "model", "model", "str"),
    _Spec("execution", "effort", "effort", "str"),
    _Spec("execution", "model_pinned", "model_pinned", "bool"),
    _Spec("execution", "effort_pinned", "effort_pinned", "bool"),
    _Spec("execution", "strict_effective", "strict_effective", "bool"),
    _Spec("runner", "mode", "runner_mode", "enum", RUNNER_MODES),
    _Spec("runner", "image", "runner_image", "str"),
    _Spec("runner", "docker_context", "docker_context", "str"),
    _Spec("runner", "model_access", "model_access", "enum", tuple(MODEL_ACCESS_MODES)),
    _Spec("validation", "commands", "validation_commands", "commands"),
    _Spec("validation", "required_checks", "required_checks", "strings+"),
    _Spec("validation", "repair_limit", "repair_limit", "int>=0"),
    _Spec("implementation", "time_budget_seconds", "time_budget_seconds", "int>0"),
    _Spec("implementation", "permitted_capabilities", "permitted_capabilities", "strings"),
    _Spec("implementation", "evidence_requirements", "evidence_requirements", "strings"),
    _Spec("protection", "paths", "protected_paths", "strings"),
    _Spec("credentials", "github", "github_credential_reference", "reference"),
    _Spec("credentials", "harness", "harness_credential_reference", "reference"),
    _Spec("executables", "github", "github_executable", "str"),
    _Spec("executables", "harness", "harness_executable", "str"),
)
CONSTRAINT_SPECS: tuple[_Spec, ...] = (
    _Spec("constraints", "allowed_harnesses", "allowed_harnesses", "strings+"),
    _Spec("constraints", "allowed_models", "allowed_models", "strings+"),
    _Spec("constraints", "allowed_efforts", "allowed_efforts", "strings+"),
    _Spec("constraints", "allowed_runner_modes", "allowed_runner_modes", "enums+", RUNNER_MODES),
    _Spec("constraints", "allowed_capabilities", "allowed_capabilities", "strings"),
    _Spec("constraints", "max_time_budget_seconds", "max_time_budget_seconds", "int>0"),
    _Spec("constraints", "max_repair_limit", "max_repair_limit", "int>=0"),
)
FIELD_NAMES: tuple[str, ...] = tuple(spec.name for spec in FIELD_SPECS)
_PROVIDER_EXECUTABLES = "provider_executables"
_ALL_SPECS = FIELD_SPECS + CONSTRAINT_SPECS
_SECTIONS: dict[str, dict[str, _Spec]] = {}
for _spec in _ALL_SPECS:
    if _spec.section is not None:
        _SECTIONS.setdefault(_spec.section, {})[_spec.key] = _spec

_CREDENTIAL_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "api-key",
    "apikey",
    "private_key",
    "private-key",
    "bearer",
)
_CREDENTIAL_VALUES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,})"
)
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class LayerDocument:
    """One validated configuration layer: the values and hard constraints it states.

    ``values`` holds only the fields the layer specifies, under their flat names;
    ``constraints`` holds its hard policy. ``source`` names where it came from (a file
    path, ``command line`` or a packaged file) and ``digest`` is the sha256 of the
    file bytes, absent for the command line.
    """

    layer: SelectionLayer
    source: str
    digest: Digest | None
    values: Mapping[str, Value] = field(default_factory=dict)
    constraints: Mapping[str, Value] = field(default_factory=dict)
    provider_executables: Mapping[str, str] = field(default_factory=dict)


class _Problem(Exception):
    def __init__(self, message: str, allowed: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.allowed = allowed


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _Problem(f"must be a string, got {type(value).__name__}")
    if not value.strip():
        raise _Problem("must be non-blank")
    return value


def _strings(value: object, *, non_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise _Problem(f"must be a list of strings, got {type(value).__name__}")
    if non_empty and not value:
        raise _Problem("must not be empty")
    return [_text(item) for item in value]


def _coerce(spec: _Spec, value: object) -> Value:
    kind = spec.kind
    if kind == "str":
        return _text(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise _Problem(f"must be true or false, got {type(value).__name__}")
        return value
    if kind in {"int>=0", "int>0"}:
        if not _is_int(value):
            raise _Problem(f"must be an integer, got {type(value).__name__}")
        assert isinstance(value, int)
        floor = 0 if kind == "int>=0" else 1
        if value < floor:
            raise _Problem(f"must be at least {floor}")
        return value
    if kind == "enum":
        text = _text(value)
        if text not in spec.allowed:
            raise _Problem(f"unsupported value {text!r}", spec.allowed)
        return text
    if kind == "reference":
        text = _text(value)
        if _REFERENCE.fullmatch(text) is None:
            raise _Problem(
                "must be an opaque credential reference name (letters, digits, '.', '_', ':', '-')"
            )
        return text
    if kind == "strings":
        return _strings(value, non_empty=False)
    if kind == "strings+":
        return _strings(value, non_empty=True)
    if kind == "enums+":
        items = _strings(value, non_empty=True)
        for item in items:
            if item not in spec.allowed:
                raise _Problem(f"unsupported value {item!r}", spec.allowed)
        return items
    if kind == "commands":
        commands = _strings(value, non_empty=True)
        try:
            split = [shlex.split(command) for command in commands]
        except ValueError as error:
            raise _Problem(f"is not a shell-splittable command: {error}") from error
        if any(not command for command in split):
            raise _Problem("must not contain an empty command")
        return split
    raise AssertionError(f"unhandled kind {kind}")  # pragma: no cover


def _scan_credentials(node: object, *, path: str, source: str) -> None:
    """Refuse a credential-shaped key or value anywhere in a project policy document."""
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            location = f"{path}.{key}" if path else str(key)
            if any(part in lowered for part in _CREDENTIAL_KEY_PARTS):
                raise ConfigError(
                    "a credential-shaped key is not allowed in project policy; keep secrets out "
                    "of tracked configuration and name only an opaque credential reference "
                    "under [credentials]",
                    file=source,
                    key=location,
                )
            _scan_credentials(value, path=location, source=source)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan_credentials(item, path=f"{path}[{index}]", source=source)
    elif isinstance(node, str) and _CREDENTIAL_VALUES.search(node):
        raise ConfigError(
            "a credential-shaped value is not allowed in project policy; keep secrets out of "
            "tracked configuration and name only an opaque credential reference "
            "under [credentials]",
            file=source,
            key=path,
        )


def parse_layer_document(
    layer: SelectionLayer,
    raw: bytes,
    *,
    source: str,
    reject_credentials: bool = False,
) -> LayerDocument:
    """Strictly parse one layer file; nothing is ignored, defaulted or coerced."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigError("the file is not valid UTF-8", file=source, key="<document>") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"the file is not valid TOML: {error}", file=source, key="<document>")
    version = data.get("schema_version")
    if version is None:
        raise ConfigError(
            "schema_version is required",
            file=source,
            key="schema_version",
            allowed=(str(LAYER_SCHEMA_VERSION),),
        )
    if not _is_int(version) or version != LAYER_SCHEMA_VERSION:
        raise ConfigError(
            f"unsupported schema version {version!r}",
            file=source,
            key="schema_version",
            allowed=(str(LAYER_SCHEMA_VERSION),),
        )
    if reject_credentials:
        _scan_credentials(data, path="", source=source)

    packaged = layer in (SelectionLayer.FACTORY, SelectionLayer.WORKFLOW)
    top_level = {"schema_version", *_SECTIONS}
    if not packaged:
        top_level.add("profile")
    if layer is SelectionLayer.FACTORY:
        top_level.add(_PROVIDER_EXECUTABLES)
    for key in data:
        if key not in top_level:
            raise ConfigError(
                "unknown key or section", file=source, key=key, allowed=tuple(sorted(top_level))
            )

    values: dict[str, Value] = {}
    constraints: dict[str, Value] = {}
    provider_executables: dict[str, str] = {}
    for spec in _ALL_SPECS:
        raw_value: object
        if spec.section is None:
            if spec.key not in data:
                continue
            raw_value = data[spec.key]
        else:
            table = data.get(spec.section)
            if table is None:
                continue
            if not isinstance(table, dict):
                raise ConfigError(
                    f"[{spec.section}] must be a table", file=source, key=spec.section
                )
            unknown = sorted(set(table) - set(_SECTIONS[spec.section]))
            if unknown:
                raise ConfigError(
                    "unknown key",
                    file=source,
                    key=f"{spec.section}.{unknown[0]}",
                    allowed=tuple(sorted(_SECTIONS[spec.section])),
                )
            if spec.key not in table:
                continue
            raw_value = table[spec.key]
        try:
            coerced = _coerce(spec, raw_value)
        except _Problem as problem:
            raise ConfigError(
                str(problem), file=source, key=spec.path, allowed=problem.allowed
            ) from problem
        (constraints if spec.section == "constraints" else values)[spec.name] = coerced
    if _PROVIDER_EXECUTABLES in data:
        table = data[_PROVIDER_EXECUTABLES]
        if not isinstance(table, dict):
            raise ConfigError(
                f"[{_PROVIDER_EXECUTABLES}] must be a table", file=source, key=_PROVIDER_EXECUTABLES
            )
        for key, item in table.items():
            try:
                provider_executables[key] = _text(item)
            except _Problem as problem:
                raise ConfigError(
                    str(problem), file=source, key=f"{_PROVIDER_EXECUTABLES}.{key}"
                ) from problem
    return LayerDocument(
        layer=layer,
        source=source,
        digest=Digest.of_bytes(raw),
        values=values,
        constraints=constraints,
        provider_executables=provider_executables,
    )


def run_layer(values: Mapping[str, object]) -> LayerDocument:
    """The run layer: the explicit command-line choices, validated like any layer file."""
    by_name = {spec.name: spec for spec in FIELD_SPECS}
    checked: dict[str, Value] = {}
    for name, raw_value in values.items():
        spec = by_name.get(name)
        if spec is None:
            raise ConfigError("unknown run override", file=RUN_SOURCE, key=name, allowed=FIELD_NAMES)
        try:
            checked[name] = _coerce(spec, raw_value)
        except _Problem as problem:
            raise ConfigError(
                str(problem), file=RUN_SOURCE, key=name, allowed=problem.allowed
            ) from problem
    return LayerDocument(layer=SelectionLayer.RUN, source=RUN_SOURCE, digest=None, values=checked)


def load_factory_layer() -> LayerDocument:
    """The packaged factory defaults: the lowest layer, one source of truth."""
    raw = (_CONFIG_DIRECTORY / FACTORY_DEFAULTS_FILE).read_bytes()
    return parse_layer_document(
        SelectionLayer.FACTORY, raw, source=f"packaged:{FACTORY_DEFAULTS_FILE}"
    )


def load_workflow_layer(name: str, *, selected_by: LayerDocument | None) -> LayerDocument:
    """The packaged workflow profile ``name``; an unknown name is refused, never defaulted."""
    filename = WORKFLOW_PROFILES.get(name)
    if filename is None:
        raise ConfigError(
            f"unknown workflow profile {name!r}",
            file=RUN_SOURCE if selected_by is None else selected_by.source,
            key="profile",
            allowed=tuple(sorted(WORKFLOW_PROFILES)),
        )
    raw = (_CONFIG_DIRECTORY / filename).read_bytes()
    return parse_layer_document(SelectionLayer.WORKFLOW, raw, source=f"packaged:{filename}")


__all__ = [
    "DEFAULT_PROFILE",
    "FIELD_NAMES",
    "FIELD_SPECS",
    "LAYER_SCHEMA_VERSION",
    "RUNNER_MODES",
    "RUN_SOURCE",
    "WORKFLOW_PROFILES",
    "ConfigError",
    "LayerDocument",
    "Value",
    "load_factory_layer",
    "load_workflow_layer",
    "parse_layer_document",
    "run_layer",
]
