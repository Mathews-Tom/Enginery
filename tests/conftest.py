from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from enginery.application.git_environment import scrub_repository_variables
from enginery.ledger.service import LedgerService
from tests.support.clock import FrozenClock
from tests.support.ids import SequentialIdGenerator

# A suite started by `git rebase --exec` (or a git hook) inherits GIT_DIR and its companions,
# which override the working directory of every fixture that runs `git init` or `git config`
# and would write the fixture's identity into the enclosing repository's shared configuration.
# Removed at import, before any test or collection-time subprocess can use them.
scrub_repository_variables()


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


@pytest.fixture(autouse=True)
def _private_xdg_directories(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the developer's real user.toml and private state out of every test."""
    root = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
