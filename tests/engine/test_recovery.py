from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLeases
from enginery.engine.recovery import RecoveryCoordinator, assess_orphan, assess_workspace_quiescence
from enginery.engine.supervisor import WorkerSupervisor, probe_process
from enginery.ledger.process_manager import ProcessManagerStateRecord
from enginery.ledger.service import LedgerService


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout


def test_workspace_lock_blocks_automatic_recovery(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    lock_path = repository / _git("rev-parse", "--git-path", "index.lock", cwd=repository).strip()
    lock_path.touch()

    assessment = assess_workspace_quiescence(repository)

    assert not assessment.ready_to_release
    assert assessment.reason == "workspace_git_lock_present"


def test_missing_process_identity_blocks_automatic_recovery(tmp_path: Path) -> None:
    record = ProcessManagerStateRecord(
        process_manager_name="worker-supervisor",
        state_key="run-1:node-1",
        state_version=1,
        state={"status": "launching"},
        updated_at=datetime.now(UTC).isoformat(),
    )

    assessment = assess_orphan(process_state=record, workspace_path=tmp_path)

    assert not assessment.ready_to_release
    assert assessment.reason == "supervisor_identity_missing_or_invalid"


def test_heartbeat_expiry_is_durably_reconciled_by_takeover(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 17, 0, tzinfo=UTC)
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    coordinator = Coordinator(ledger_service, owner="first")
    first_epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    lease = FencedNodeLeases(ledger_service, coordinator).grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=first_epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=120),
        expected_attempt_version=0,
    )
    supervisor = WorkerSupervisor(ledger_service, coordinator)
    identity = supervisor.start(
        lease=lease,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        cwd=repository,
        now=now,
    )

    assert supervisor.enforce_heartbeat(
        lease=lease, identity=identity, now=now + timedelta(seconds=61)
    )
    assert probe_process(identity.pid) is None

    takeover = Coordinator(ledger_service, owner="second")
    second_epoch = takeover.acquire(
        now=now + timedelta(seconds=62), heartbeat_window=timedelta(seconds=60)
    )
    assessment = RecoveryCoordinator(ledger_service, takeover).reconcile(
        lease=lease,
        workspace_path=repository,
        epoch=second_epoch.epoch,
        now=now + timedelta(seconds=62),
    )

    assert assessment.ready_to_release
    state = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert state is not None
    assert state.state["status"] == "exit_observed"
    assert state.state["reconciled_epoch"] == 2
