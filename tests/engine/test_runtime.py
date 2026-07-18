from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.runtime import FixtureDispatch, FixtureRuntime
from enginery.engine.supervisor import WorkerSupervisor, probe_process
from enginery.engine.workspace import GitWorktreeBackend
from enginery.ledger.service import LedgerService


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def test_dispatch_persists_workspace_lease_and_supervised_worker(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    repository, base_revision = _repository(tmp_path)
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    runtime = FixtureRuntime(ledger_service, coordinator)
    request = FixtureDispatch(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-1",
    )

    dispatched = runtime.dispatch(
        request=request,
        epoch=epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=30),
    )

    assert dispatched.workspace.workspace_path.is_dir()
    assert ledger_service.read_lease(run_id="run-1", node_id="node-1") is not None
    assert probe_process(dispatched.identity.pid) == dispatched.identity

    WorkerSupervisor(ledger_service, coordinator).cancel(
        lease=dispatched.lease,
        identity=dispatched.identity,
        now=now + timedelta(seconds=1),
    )
    cleaned = GitWorktreeBackend(ledger_service, coordinator).cleanup(
        dispatched.workspace, epoch=epoch.epoch, now=now + timedelta(seconds=2)
    )
    assert cleaned.status == "cleaned"


def test_repository_reservation_blocks_second_run(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    repository, base_revision = _repository(tmp_path)
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    runtime = FixtureRuntime(ledger_service, coordinator)
    first = FixtureDispatch(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-1",
    )
    dispatched = runtime.dispatch(
        request=first,
        epoch=epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=30),
    )
    second = FixtureDispatch(
        run_id="run-2",
        node_id="node-1",
        attempt_id="attempt-2",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-2",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-2",
    )

    with pytest.raises(ExternalConflictError, match="reserved by another run"):
        runtime.dispatch(
            request=second,
            epoch=epoch.epoch,
            now=now,
            lease_window=timedelta(seconds=30),
        )
    WorkerSupervisor(ledger_service, coordinator).cancel(
        lease=dispatched.lease,
        identity=dispatched.identity,
        now=now + timedelta(seconds=1),
    )
    GitWorktreeBackend(ledger_service, coordinator).cleanup(
        dispatched.workspace, epoch=epoch.epoch, now=now + timedelta(seconds=2)
    )


def test_lease_boundary_fault_leaves_no_unrecorded_worker(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    repository, base_revision = _repository(tmp_path)
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))

    def _interrupt(point: str) -> None:
        if point == "lease_granted":
            raise RuntimeError("injected coordinator crash")

    request = FixtureDispatch(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-1",
        base_revision=base_revision,
        command=("must-not-start",),
        expected_attempt_version=0,
        operation_id="operation-1",
    )

    with pytest.raises(RuntimeError, match="injected coordinator crash"):
        FixtureRuntime(ledger_service, coordinator, fault_hook=_interrupt).dispatch(
            request=request,
            epoch=epoch.epoch,
            now=now,
            lease_window=timedelta(seconds=30),
        )

    lease = ledger_service.read_lease(run_id="run-1", node_id="node-1")
    assert lease is not None
    assert (tmp_path / "workspace-1").is_dir()
    state = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert state is None
