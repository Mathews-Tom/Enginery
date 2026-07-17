from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from enginery.ledger.service import LedgerService
from tests.support.clock import FrozenClock
from tests.support.ids import SequentialIdGenerator


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def id_generator() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def ledger_service(ledger_path: Path) -> Iterator[LedgerService]:
    service = LedgerService.open(ledger_path)
    try:
        yield service
    finally:
        service.close()
