"""Deterministic ID generator test helper.

Domain and engine code that needs stable operation/aggregate identity (added
in a later milestone) will accept an ID-generator port; this fixture stands
in for it in tests so generated identifiers are stable and ordered.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import count


@dataclass
class SequentialIdGenerator:
    """Produces stable, ordered, prefixed identifiers."""

    prefix: str = "id"
    _counter: Iterator[int] = field(default_factory=lambda: count(1))

    def next_id(self) -> str:
        return f"{self.prefix}-{next(self._counter):06d}"
