from __future__ import annotations

import pytest

from tests.support.clock import FrozenClock
from tests.support.ids import SequentialIdGenerator


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def id_generator() -> SequentialIdGenerator:
    return SequentialIdGenerator()
