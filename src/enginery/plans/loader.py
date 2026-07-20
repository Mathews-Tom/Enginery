"""The plan schema adapter: turns a TOML plan file into a validated ``Plan``.

TOML is the on-disk plan representation, mirroring how a workflow manifest
is authored as a repository-owned file rather than embedded code. Parsing
is a two-step translation, matching every other external-representation
adapter in this codebase: ``tomllib`` turns bytes into a plain mapping,
then ``Plan.from_mapping`` normalizes and validates that mapping into the
closed domain schema. This module never executes, schedules, or persists
anything — it only produces a ``Plan`` value or raises.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from enginery.domain.errors import InvalidInputError
from enginery.plans.model import Plan


def parse_plan_toml(text: str) -> Plan:
    """Parse an in-memory TOML document into a validated ``Plan``."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise InvalidInputError(f"plan document is not valid TOML: {error}") from error
    return Plan.from_mapping(raw)


def load_plan(path: Path) -> Plan:
    """Read and parse a TOML plan file from disk into a validated ``Plan``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidInputError(
            f"plan file could not be read: {error}", details={"path": str(path)}
        ) from error
    return parse_plan_toml(text)


__all__ = ["load_plan", "parse_plan_toml"]
