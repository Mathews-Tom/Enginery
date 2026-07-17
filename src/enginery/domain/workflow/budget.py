"""Retry, timeout, and cost budget declarations for workflow nodes.

Every side-effecting node declares bounded retry, time, and cost limits so
a run can never retry or spend unboundedly; invalid limits are rejected at
construction time rather than discovered at execution time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError

_BUDGET_KEYS = frozenset({"max_attempts", "max_duration_seconds", "max_cost"})


@dataclass(frozen=True, slots=True)
class Budget:
    """A node's retry, timeout, and cost ceiling."""

    max_attempts: int = 1
    max_duration_seconds: float = 60.0
    max_cost: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InvalidInputError(
                "max_attempts must be at least 1", details={"max_attempts": self.max_attempts}
            )
        if self.max_duration_seconds <= 0:
            raise InvalidInputError(
                "max_duration_seconds must be positive",
                details={"max_duration_seconds": self.max_duration_seconds},
            )
        if self.max_cost is not None and self.max_cost < 0:
            raise InvalidInputError(
                "max_cost cannot be negative", details={"max_cost": self.max_cost}
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Budget:
        unknown_keys = set(raw) - _BUDGET_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "budget declares unknown keys; manifests cannot embed executable payloads",
                details={"unknown_keys": sorted(unknown_keys)},
            )
        kwargs: dict[str, object] = {}
        if "max_attempts" in raw:
            kwargs["max_attempts"] = raw["max_attempts"]
        if "max_duration_seconds" in raw:
            kwargs["max_duration_seconds"] = raw["max_duration_seconds"]
        if "max_cost" in raw:
            kwargs["max_cost"] = raw["max_cost"]
        return cls(**kwargs)  # type: ignore[arg-type]


__all__ = ["Budget"]
