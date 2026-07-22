"""The registered gate readiness floor and human-principal roster.

A gate condition whose registered floor has not yet been set by a human
review is *unmeasurable*, not merely absent -- this module distinguishes
"no floor registered yet" (``None``) from "floor registered as zero"
(present with an explicit value) so :mod:`enginery.evaluation.gate` can
report ``unmeasured`` rather than silently treating a missing floor as
satisfied.

Editing the on-disk TOML file directly is the only way a floor is
registered or a human principal is added to the roster; nothing in this
codebase mutates it programmatically, matching
``config/performance-bounds.toml``'s existing human-maintained-floor
convention (``scripts/performance_baseline.py`` reads it the same way).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from enginery.domain.errors import InvalidInputError

GATE_FLOOR_SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "registered_principals",
        "completed_runs",
        "interventions",
        "outcome_completeness",
    }
)


@dataclass(frozen=True, slots=True)
class GateFloorConfig:
    """A versioned, human-maintained readiness floor and principal roster."""

    schema_version: int
    registered_principal_ids: tuple[str, ...]
    completed_run_volume_floor: int | None
    intervention_volume_floor: int | None
    outcome_completeness_floor: float | None


def load_gate_floor_config(path: Path) -> GateFloorConfig:
    """Load and validate one gate-floor configuration file.

    Fails closed: a missing file, malformed TOML, unsupported schema
    version, or a malformed field raises rather than silently falling
    back to an empty configuration.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidInputError(
            "unable to read gate floor configuration", details={"path": str(path)}
        ) from error
    try:
        raw: dict[str, object] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise InvalidInputError(
            "gate floor configuration is not valid TOML",
            details={"path": str(path), "error": str(error)},
        ) from error
    unknown_keys = set(raw) - _TOP_LEVEL_KEYS
    if unknown_keys:
        raise InvalidInputError(
            "gate floor configuration has unrecognized top-level keys",
            details={"path": str(path), "keys": sorted(unknown_keys)},
        )
    schema_version = raw.get("schema_version")
    if schema_version != GATE_FLOOR_SCHEMA_VERSION:
        raise InvalidInputError(
            "unsupported gate floor configuration schema version",
            details={"path": str(path), "schema_version": schema_version},
        )
    principals = _table(raw, "registered_principals", path=path)
    completed_runs = _table(raw, "completed_runs", path=path)
    interventions = _table(raw, "interventions", path=path)
    outcome_completeness = _table(raw, "outcome_completeness", path=path)
    return GateFloorConfig(
        schema_version=GATE_FLOOR_SCHEMA_VERSION,
        registered_principal_ids=tuple(dict.fromkeys(_string_list(principals, "ids", path=path))),
        completed_run_volume_floor=_optional_int(completed_runs, "min_total", path=path),
        intervention_volume_floor=_optional_int(interventions, "min_with_reason", path=path),
        outcome_completeness_floor=_optional_fraction(outcome_completeness, "floor", path=path),
    )


def _table(raw: dict[str, object], key: str, *, path: Path) -> dict[str, object]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise InvalidInputError(
            f"gate floor configuration `{key}` must be a table",
            details={"path": str(path), "key": key},
        )
    return value


def _string_list(table: dict[str, object], key: str, *, path: Path) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in value
    ):
        raise InvalidInputError(
            f"gate floor configuration `{key}` must be a list of non-blank strings",
            details={"path": str(path), "key": key},
        )
    return list(value)


def _optional_int(table: dict[str, object], key: str, *, path: Path) -> int | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidInputError(
            f"gate floor configuration `{key}` must be a non-negative integer",
            details={"path": str(path), "key": key},
        )
    return value


def _optional_fraction(table: dict[str, object], key: str, *, path: Path) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInputError(
            f"gate floor configuration `{key}` must be a number",
            details={"path": str(path), "key": key},
        )
    fraction = float(value)
    if not 0.0 <= fraction <= 1.0:
        raise InvalidInputError(
            f"gate floor configuration `{key}` must be between 0 and 1",
            details={"path": str(path), "key": key},
        )
    return fraction


__all__ = ["GATE_FLOOR_SCHEMA_VERSION", "GateFloorConfig", "load_gate_floor_config"]
