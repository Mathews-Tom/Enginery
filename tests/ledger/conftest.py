from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from enginery.ledger.service import LedgerService


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
