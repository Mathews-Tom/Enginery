from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLeases
from enginery.engine.supervisor import WorkerSupervisor, probe_process
from enginery.ledger.service import LedgerService


def test_cancellation_terminates_supervised_process_group(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 18, 15, 0, tzinfo=UTC)
    coordinator = Coordinator(ledger_service, owner="coordinator")
    epoch = coordinator.acquire(now=now, heartbeat_window=timedelta(seconds=60))
    lease = FencedNodeLeases(ledger_service, coordinator).grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=epoch.epoch,
        now=now,
        lease_window=timedelta(seconds=30),
        expected_attempt_version=0,
    )
    supervisor = WorkerSupervisor(ledger_service, coordinator)
    identity = supervisor.start(
        lease=lease,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        cwd=tmp_path,
        now=now,
    )

    supervisor.cancel(lease=lease, identity=identity, now=now + timedelta(seconds=1))

    assert probe_process(identity.pid) is None
