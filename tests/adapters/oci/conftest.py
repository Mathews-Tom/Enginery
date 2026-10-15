from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.domain.runner import ResourceLimits, RunnerProfile

from .real_engine import build_standin_image, remove_attempt, require_real_engine
from .support import FakeDocker


@pytest.fixture
def fake_docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture(scope="session")
def real_engine() -> OciEngine:
    return require_real_engine()


@pytest.fixture(scope="session")
def standin_image(
    real_engine: OciEngine, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """Image id of the TEST-ONLY STAND-IN worker image (see ``real_engine``)."""
    tag, image_id = build_standin_image(real_engine, tmp_path_factory.mktemp("standin"))
    yield image_id
    with contextlib.suppress(Exception):
        real_engine.run_cli(("image", "rm", tag))


@pytest.fixture
def real_operation(
    real_engine: OciEngine, real_profile: RunnerProfile
) -> Iterator[Callable[[], str]]:
    """Mint operation ids unique to this test; their containers are removed afterwards.

    Only containers created for these ids are removed, never other runner containers that
    may exist on the engine.
    """
    minted: list[str] = []

    def mint() -> str:
        operation_id = f"test-{uuid.uuid4().hex[:12]}"
        minted.append(operation_id)
        return operation_id

    yield mint
    for operation_id in minted:
        remove_attempt(real_engine, operation_id)


@pytest.fixture
def real_profile(real_engine: OciEngine) -> Iterator[RunnerProfile]:
    """A profile on a throw-away internal network, with limits small enough for any engine."""
    network = f"enginery-test-{uuid.uuid4().hex[:10]}"
    profile = RunnerProfile(
        network=network,
        resources=ResourceLimits(
            memory_mib=256, cpus=1.0, pids=128, tmp_mib=32, home_mib=32, stop_grace_seconds=2
        ),
    )
    try:
        yield profile
    finally:
        real_engine.run_cli(("network", "rm", network))


@pytest.fixture
def real_root(tmp_path: Path) -> Path:
    """A test directory resolved through symlinks so the engine's file sharing sees it."""
    return tmp_path.resolve()
