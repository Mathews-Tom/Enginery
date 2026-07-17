from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support.clock import FrozenClock
from tests.support.ids import SequentialIdGenerator


def test_frozen_clock_does_not_advance_on_its_own(frozen_clock: FrozenClock) -> None:
    first = frozen_clock.now()
    second = frozen_clock.now()

    assert first == second


def test_frozen_clock_advance_moves_forward_by_delta(frozen_clock: FrozenClock) -> None:
    start = frozen_clock.now()

    result = frozen_clock.advance(timedelta(hours=1))

    assert result == start + timedelta(hours=1)
    assert frozen_clock.now() == start + timedelta(hours=1)


def test_frozen_clock_set_requires_timezone_aware_datetime(frozen_clock: FrozenClock) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        frozen_clock.set(datetime(2030, 1, 1))


def test_frozen_clock_set_updates_now(frozen_clock: FrozenClock) -> None:
    target = datetime(2030, 6, 15, tzinfo=UTC)

    frozen_clock.set(target)

    assert frozen_clock.now() == target


def test_sequential_id_generator_produces_stable_ordered_ids(
    id_generator: SequentialIdGenerator,
) -> None:
    assert id_generator.next_id() == "id-000001"
    assert id_generator.next_id() == "id-000002"


def test_sequential_id_generator_uses_configured_prefix() -> None:
    generator = SequentialIdGenerator(prefix="run")

    assert generator.next_id() == "run-000001"
