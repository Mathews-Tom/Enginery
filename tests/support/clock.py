"""Deterministic clock test helper.

Domain and engine code that needs "now" will accept a clock port (added in
a later milestone); this fixture stands in for it in tests so behavior
never depends on wall-clock time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class FrozenClock:
    """A clock that only advances when told to."""

    _now: datetime = field(default_factory=lambda: _EPOCH)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._now = value
