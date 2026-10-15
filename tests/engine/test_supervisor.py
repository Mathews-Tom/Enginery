from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.supervisor import WorkerSupervisor, probe_process
from enginery.ledger.service import LedgerService


def _lease(ledger_service: LedgerService, *, now: datetime) -> tuple[Coordinator, FencedNodeLease]:
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
        operation_id="operation-1",
    )
    return coordinator, lease


def test_launch_failure_is_durable_before_process_creation(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 18, 15, 0, tzinfo=UTC)
    coordinator, lease = _lease(ledger_service, now=now)

    def _fail(*_: object, **__: object) -> subprocess.Popen[bytes]:
        raise OSError("cannot spawn")

    monkeypatch.setattr("enginery.engine.supervisor.subprocess.Popen", _fail)

    with pytest.raises(ExternalConflictError, match="launch failed"):
        WorkerSupervisor(ledger_service, coordinator).start(
            lease=lease, command=("worker",), cwd=tmp_path, now=now
        )

    state = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert state is not None
    assert state.state["status"] == "launch_failed"


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
        operation_id="operation-1",
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


_DUMP_ENVIRONMENT = (
    "import json, os, sys; open(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))"
)


def _run_worker_and_read_environment(
    ledger_service: LedgerService, tmp_path: Path, *, environment: dict[str, str] | None
) -> dict[str, str]:
    now = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
    coordinator, lease = _lease(ledger_service, now=now)
    dump = tmp_path / "environment.json"
    identity = WorkerSupervisor(ledger_service, coordinator).start(
        lease=lease,
        command=(sys.executable, "-c", _DUMP_ENVIRONMENT, str(dump)),
        cwd=tmp_path,
        now=now,
        environment=environment,
    )
    # A later Popen in this process may already have reaped the exited child.
    with contextlib.suppress(ChildProcessError):
        os.waitpid(identity.pid, 0)
    loaded = json.loads(dump.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in loaded.items()}


def test_a_worker_started_with_an_environment_receives_exactly_that_environment(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    monkeypatch.setenv("ENGINERY_AMBIENT_MARKER", "ambient")

    received = _run_worker_and_read_environment(
        ledger_service, tmp_path, environment={"PATH": os.environ["PATH"], "PURPOSE": "explicit"}
    )

    assert received["PURPOSE"] == "explicit"
    assert received["PATH"] == os.environ["PATH"]
    assert "GH_TOKEN" not in received
    assert "ENGINERY_AMBIENT_MARKER" not in received
    assert set(received) <= {"PATH", "PURPOSE", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}


def test_a_worker_started_without_an_environment_inherits_the_ambient_one_the_legacy_path(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENGINERY_AMBIENT_MARKER", "ambient")

    received = _run_worker_and_read_environment(ledger_service, tmp_path, environment=None)

    assert received["ENGINERY_AMBIENT_MARKER"] == "ambient"


def test_an_empty_environment_is_an_empty_environment_not_the_ambient_one(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENGINERY_AMBIENT_MARKER", "ambient")

    received = _run_worker_and_read_environment(ledger_service, tmp_path, environment={})

    assert "ENGINERY_AMBIENT_MARKER" not in received
    assert set(received) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
