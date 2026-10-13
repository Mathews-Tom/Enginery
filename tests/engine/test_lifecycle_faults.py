"""Lifecycle faults against real SQLite ledgers, git worktrees and worker subprocesses.

Cancellation after a coordinator restart, a worker launched by a different OS process, stale
coordinator fencing, and repeated evaluation that must never duplicate a launch.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError
from enginery.engine.leases import FencedNodeLeases
from enginery.engine.recovery import RecoveryCoordinator
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import (
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
    FixtureRuntime,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.supervisor import WorkerSupervisor, probe_process
from enginery.ledger.service import LedgerService

_WINDOW = timedelta(seconds=60)
_LEASE = timedelta(seconds=600)
_ONE_SLOT = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
_SLEEP = (sys.executable, "-c", "import time; time.sleep(120)")
_EXIT = (sys.executable, "-c", "pass")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path, name: str) -> tuple[Path, str]:
    repository = tmp_path / name
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _node(tmp_path: Path, name: str, *, command: tuple[str, ...]) -> FixtureDispatch:
    repository, revision = _repository(tmp_path, f"repository-{name}")
    return FixtureDispatch(
        run_id=f"run-{name}",
        node_id="work",
        attempt_id=f"attempt-{name}",
        repository_id=f"repository-{name}",
        repository_path=repository,
        workspace_path=tmp_path / f"workspace-{name}",
        base_revision=revision,
        command=command,
        expected_attempt_version=0,
        operation_id=f"operation-{name}",
    )


def _tick(
    runtime: CoordinatorRuntime, now: datetime, *requests: FixtureDispatch
) -> tuple[DispatchedFixture, ...]:
    return runtime.tick(
        now=now,
        heartbeat_window=_WINDOW,
        lease_window=_LEASE,
        limits=_ONE_SLOT,
        requests=requests,
    ).dispatched


def _status(ledger: LedgerService, name: str) -> object:
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id=f"run-{name}:work")
    assert node is not None
    return node.state["status"]


def _events(ledger: LedgerService, event_type: str, name: str) -> int:
    row = ledger.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND aggregate_id = ?",
        (event_type, f"run-{name}:work"),
    ).fetchone()
    return int(row[0])


def _supervisor_state(ledger: LedgerService, name: str) -> dict[str, object]:
    record = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key=f"run-{name}:work"
    )
    assert record is not None
    return dict(record.state)


def test_a_running_worker_is_cancelled_by_a_restarted_coordinator_and_its_evidence_is_kept(
    tmp_path: Path, ledger_path: Path
) -> None:
    node = _node(tmp_path, "work", command=_SLEEP)
    ledger = LedgerService.open(ledger_path)
    try:
        (running,) = _tick(CoordinatorRuntime(ledger, owner="operator"), _T0, node)
        assert running.lease.epoch == 1
    finally:
        ledger.close()

    # The coordinator restarts after its heartbeat window lapsed: a newer epoch owns the
    # ledger, while the worker still holds the lease the first epoch issued.
    ledger = LedgerService.open(ledger_path)
    try:
        restarted = CoordinatorRuntime(ledger, owner="operator")
        epoch = restarted.claim_epoch(now=_T0 + timedelta(seconds=120), heartbeat_window=_WINDOW)
        assert epoch.epoch == 2

        with pytest.raises(ExternalConflictError, match="cancellation epoch"):
            restarted.cancel_node(
                run_id="run-work",
                node_id="work",
                epoch=1,
                now=_T0 + timedelta(seconds=121),
            )
        assert probe_process(running.identity.pid) == running.identity
        assert _status(ledger, "work") == "running"

        restarted.cancel_node(
            run_id="run-work",
            node_id="work",
            epoch=epoch.epoch,
            now=_T0 + timedelta(seconds=122),
        )

        assert probe_process(running.identity.pid) is None
        assert _status(ledger, "work") == "cancelled"
        # Evidence survives: the identity the worker was killed under, both termination
        # events, and the lease the first epoch issued -- retired, not rewritten.
        recorded = _supervisor_state(ledger, "work")
        assert recorded["pid"] == running.identity.pid
        assert recorded["start_identity"] == running.identity.start_identity
        assert recorded["status"] == "exit_reconciled"
        assert _events(ledger, "worker.termination_requested", "work") == 1
        assert _events(ledger, "worker.cancelled", "work") == 1
        lease = ledger.read_lease(run_id="run-work", node_id="work")
        assert lease is not None
        assert (lease.epoch, lease.fencing_token) == (1, 1)
        assert lease.expires_at == (_T0 + timedelta(seconds=122)).isoformat()
        reservation = restarted.read_workspace_reservation("repository-work")
        assert reservation is not None
        assert reservation.status == "cleaned"
    finally:
        ledger.close()


def test_a_worker_launched_by_an_earlier_process_is_terminated_by_a_later_one(
    tmp_path: Path, ledger_path: Path
) -> None:
    node = _node(tmp_path, "work", command=_SLEEP)
    launcher = textwrap.dedent(
        """
        import sys
        from datetime import UTC, datetime, timedelta
        from pathlib import Path
        from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
        from enginery.engine.scheduler import SchedulingLimits
        from enginery.ledger.service import LedgerService

        ledger_path, repository, workspace, revision = sys.argv[1:5]
        ledger = LedgerService.open(Path(ledger_path))
        request = FixtureDispatch(
            run_id="run-work", node_id="work", attempt_id="attempt-work",
            repository_id="repository-work", repository_path=Path(repository),
            workspace_path=Path(workspace), base_revision=revision,
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            expected_attempt_version=0, operation_id="operation-work",
        )
        tick = CoordinatorRuntime(ledger, owner="operator").tick(
            now=datetime.now(UTC), heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(minutes=10), limits=SchedulingLimits(1, 1),
            requests=(request,),
        )
        print(tick.dispatched[0].identity.pid)
        ledger.close()
        """
    )
    # Redirect to a file, not a pipe: the orphaned worker inherits the launcher's stdout, and
    # a pipe would not reach EOF until that worker exits.
    launcher_output = tmp_path / "launcher.out"
    with launcher_output.open("w", encoding="utf-8") as stdout:
        subprocess.run(
            [
                sys.executable,
                "-c",
                launcher,
                str(ledger_path),
                str(node.repository_path),
                str(node.workspace_path),
                node.base_revision,
            ],
            stdout=stdout,
            check=True,
        )
    pid = int(launcher_output.read_text(encoding="utf-8").strip())
    ledger = LedgerService.open(ledger_path)
    try:
        # The launching process is gone, so this process is not the worker's parent and
        # cannot wait on it: termination must still complete and be recorded.
        assert probe_process(pid) is not None
        runtime = CoordinatorRuntime(ledger, owner="operator")
        now = datetime.now(UTC)
        epoch = runtime.claim_epoch(now=now, heartbeat_window=_WINDOW)

        runtime.cancel_node(run_id="run-work", node_id="work", epoch=epoch.epoch, now=now)

        assert probe_process(pid) is None
        assert _status(ledger, "work") == "cancelled"
        assert _supervisor_state(ledger, "work")["status"] == "exit_reconciled"
        assert _events(ledger, "worker.cancelled", "work") == 1
    finally:
        ledger.close()


def test_a_cancelled_queued_node_keeps_its_wakeup_evidence_and_is_never_dispatched(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    occupier = _node(tmp_path, "occupier", command=_SLEEP)
    queued = _node(tmp_path, "queued", command=_SLEEP)
    (held,) = _tick(runtime, _T0, occupier)
    assert _tick(runtime, _T0 + timedelta(seconds=1), queued) == ()
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=2), heartbeat_window=_WINDOW)

    runtime.cancel_node(
        run_id="run-queued",
        node_id="work",
        epoch=epoch.epoch,
        now=_T0 + timedelta(seconds=3),
    )

    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-queued:work"
    )
    assert node is not None
    assert node.state["status"] == "cancelled"
    assert node.state["wakeup"]["reason"] == "global_capacity"  # type: ignore[index]
    assert node.state["operation_id"] == "operation-queued"
    # Releasing capacity afterwards cannot revive the cancelled intent.
    runtime.cancel_node(
        run_id="run-occupier",
        node_id="work",
        epoch=held.lease.epoch,
        now=_T0 + timedelta(seconds=4),
    )
    assert _tick(runtime, _T0 + timedelta(seconds=5)) == ()
    assert ledger_service.read_lease(run_id="run-queued", node_id="work") is None
    assert _events(ledger_service, "worker.launch_intended", "queued") == 0


def test_a_stale_coordinator_cannot_dispatch_or_ingest(tmp_path: Path, ledger_path: Path) -> None:
    exiting = _node(tmp_path, "exiting", command=_EXIT)
    pending = _node(tmp_path, "pending", command=_SLEEP)
    ledger = LedgerService.open(ledger_path)
    try:
        stale = CoordinatorRuntime(ledger, owner="operator")
        (first,) = _tick(stale, _T0, exiting)
        assert first.lease.epoch == 1
        # A successor with the same owner name takes epoch 2 after the heartbeat lapsed.
        successor = CoordinatorRuntime(ledger, owner="operator")
        later = _T0 + timedelta(seconds=120)
        assert successor.claim_epoch(now=later, heartbeat_window=_WINDOW).epoch == 2

        # The stale coordinator still believes it holds epoch 1.
        with pytest.raises(ExternalConflictError, match="not the current owner"):
            FixtureRuntime(ledger, stale.coordinator).dispatch(
                request=pending,
                epoch=1,
                now=later + timedelta(seconds=1),
                lease_window=_LEASE,
            )
        assert ledger.read_lease(run_id="run-pending", node_id="work") is None
        assert _events(ledger, "worker.launch_intended", "pending") == 0
        assert successor.read_workspace_reservation("repository-pending") is None

        stale_supervisor = WorkerSupervisor(ledger, stale.coordinator)
        deadline = time.monotonic() + 10
        while True:
            with pytest.raises(ExternalConflictError) as refused:
                stale_supervisor.observe_exit(
                    lease=first.lease, coordinator_epoch=1, now=later + timedelta(seconds=1)
                )
            if "not the current owner" in str(refused.value):
                break
            assert "before supervised process exit" in str(refused.value)
            assert time.monotonic() < deadline, "the exiting worker was never observed"
            time.sleep(0.05)
        assert _supervisor_state(ledger, "exiting")["status"] == "running"

        WorkerSupervisor(ledger, successor.coordinator).observe_exit(
            lease=first.lease, coordinator_epoch=2, now=later + timedelta(seconds=2)
        )
        envelope = WorkerResultEnvelope(
            run_id=first.lease.run_id,
            node_id=first.lease.node_id,
            attempt_id=first.lease.attempt_id,
            epoch=first.lease.epoch,
            fencing_token=first.lease.fencing_token,
            operation_id=first.lease.operation_id,
            terminal_result="passed",
            artifact_references=(),
            result={},
        )
        attempt = ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=first.lease.attempt_id
        )
        assert attempt is not None
        with pytest.raises(ExternalConflictError, match="not the current owner"):
            FencedNodeLeases(ledger, stale.coordinator).ingest_result(
                envelope=envelope,
                coordinator_epoch=1,
                now=later + timedelta(seconds=3),
                expected_attempt_version=attempt.aggregate_version,
            )
        unchanged = ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=first.lease.attempt_id
        )
        assert unchanged is not None
        assert unchanged.aggregate_version == attempt.aggregate_version

        # The identical envelope is accepted under the current epoch: only fencing refused it.
        FencedNodeLeases(ledger, successor.coordinator).ingest_result(
            envelope=envelope,
            coordinator_epoch=2,
            now=later + timedelta(seconds=4),
            expected_attempt_version=attempt.aggregate_version,
        )
    finally:
        ledger.close()


def test_a_different_coordinator_cannot_tick_while_another_epoch_is_active(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    owner = CoordinatorRuntime(ledger_service, owner="operator")
    intruder = CoordinatorRuntime(ledger_service, owner="intruder")
    pending = _node(tmp_path, "pending", command=_SLEEP)
    owner.claim_epoch(now=_T0, heartbeat_window=_WINDOW)

    with pytest.raises(ExternalConflictError, match="another coordinator owns"):
        _tick(intruder, _T0 + timedelta(seconds=1), pending)

    assert ledger_service.read_lease(run_id="run-pending", node_id="work") is None
    assert _events(ledger_service, "worker.launch_intended", "pending") == 0


def test_repeated_ticks_and_recovery_never_duplicate_a_launch(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    node = _node(tmp_path, "work", command=_SLEEP)
    (running,) = _tick(runtime, _T0, node)

    for offset in range(1, 6):
        assert _tick(runtime, _T0 + timedelta(seconds=offset), node) == ()

    with pytest.raises(ExternalConflictError, match="already has an active lease"):
        FixtureRuntime(ledger_service, runtime.coordinator).dispatch(
            request=node,
            epoch=running.lease.epoch,
            now=_T0 + timedelta(seconds=6),
            lease_window=_LEASE,
        )
    coordinator = runtime.coordinator
    with pytest.raises(ExternalConflictError, match="already has an active lease"):
        FencedNodeLeases(ledger_service, coordinator).grant(
            run_id="run-work",
            node_id="work",
            attempt_id="attempt-work",
            epoch=running.lease.epoch,
            now=_T0 + timedelta(seconds=7),
            lease_window=_LEASE,
            expected_attempt_version=0,
            operation_id="operation-work",
        )
    with pytest.raises(ExternalConflictError, match="not been reconciled"):
        WorkerSupervisor(ledger_service, coordinator).start(
            lease=running.lease,
            command=_SLEEP,
            cwd=running.workspace.workspace_path,
            now=_T0 + timedelta(seconds=8),
        )
    # Recovery of a worker that is alive proves nothing is orphaned and touches nothing.
    assessment = RecoveryCoordinator(ledger_service, coordinator).reconcile(
        lease=running.lease,
        workspace_path=running.workspace.workspace_path,
        epoch=running.lease.epoch,
        now=_T0 + timedelta(seconds=9),
    )
    assert not assessment.ready_to_release
    assert assessment.reason == "process_still_running"

    assert _events(ledger_service, "worker.launch_intended", "work") == 1
    assert _events(ledger_service, "worker.started", "work") == 1
    assert probe_process(running.identity.pid) == running.identity
    assert _supervisor_state(ledger_service, "work")["status"] == "running"
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=10), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=11)
    )


def test_a_launching_process_returns_promptly_while_its_worker_keeps_running(
    tmp_path: Path, ledger_path: Path
) -> None:
    node = _node(tmp_path, "work", command=_SLEEP)
    launcher = textwrap.dedent(
        """
        import sys
        from datetime import UTC, datetime, timedelta
        from pathlib import Path
        from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
        from enginery.engine.scheduler import SchedulingLimits
        from enginery.ledger.service import LedgerService

        ledger_path, repository, workspace, revision = sys.argv[1:5]
        ledger = LedgerService.open(Path(ledger_path))
        request = FixtureDispatch(
            run_id="run-work", node_id="work", attempt_id="attempt-work",
            repository_id="repository-work", repository_path=Path(repository),
            workspace_path=Path(workspace), base_revision=revision,
            command=(sys.executable, "-c", "import time; time.sleep(120)"),
            expected_attempt_version=0, operation_id="operation-work",
        )
        tick = CoordinatorRuntime(ledger, owner="operator").tick(
            now=datetime.now(UTC), heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(minutes=10), limits=SchedulingLimits(1, 1),
            requests=(request,),
        )
        print(tick.dispatched[0].identity.pid)
        ledger.close()
        """
    )
    started = time.monotonic()
    # Piped stdout, as in `stage1 watch --advance | jq`: EOF must arrive when the
    # launcher exits, not when the worker it started exits.
    launched = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            str(ledger_path),
            str(node.repository_path),
            str(node.workspace_path),
            node.base_revision,
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    elapsed = time.monotonic() - started
    pid = int(launched.stdout.strip())
    try:
        assert elapsed < 30
        assert probe_process(pid) is not None
    finally:
        ledger = LedgerService.open(ledger_path)
        try:
            runtime = CoordinatorRuntime(ledger, owner="operator")
            now = datetime.now(UTC)
            epoch = runtime.claim_epoch(now=now, heartbeat_window=_WINDOW)
            runtime.cancel_node(run_id="run-work", node_id="work", epoch=epoch.epoch, now=now)
        finally:
            ledger.close()
