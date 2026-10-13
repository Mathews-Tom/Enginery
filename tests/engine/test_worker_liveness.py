"""Worker death, retryable cancellation, group termination and lease-epoch liveness.

Every scenario runs real worker subprocesses against a real SQLite ledger and git worktrees.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import ExternalConflictError, InternalInvariantViolationError
from enginery.engine import supervisor as supervisor_module
from enginery.engine.runtime import CoordinatorRuntime, DispatchedFixture, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.supervisor import ProcessIdentity, probe_process
from enginery.ledger.service import LedgerService

_WINDOW = timedelta(seconds=60)
_LEASE = timedelta(seconds=600)
_ONE_SLOT = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
_SLEEP = (sys.executable, "-c", "import time; time.sleep(120)")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _node(
    tmp_path: Path,
    name: str,
    *,
    command: tuple[str, ...] = _SLEEP,
    log_directory: Path | None = None,
) -> FixtureDispatch:
    repository = tmp_path / f"repository-{name}"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return FixtureDispatch(
        run_id=f"run-{name}",
        node_id="work",
        attempt_id=f"attempt-{name}",
        repository_id=f"repository-{name}",
        repository_path=repository,
        workspace_path=tmp_path / f"workspace-{name}",
        base_revision=_git("rev-parse", "HEAD", cwd=repository),
        command=command,
        expected_attempt_version=0,
        operation_id=f"operation-{name}",
        retain_workspace=True,
        log_directory=log_directory,
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


def _events(ledger: LedgerService, event_type: str, name: str) -> int:
    row = ledger.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND aggregate_id = ?",
        (event_type, f"run-{name}:work"),
    ).fetchone()
    return int(row[0])


def _state(ledger: LedgerService, name: str) -> dict[str, object]:
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id=f"run-{name}:work")
    assert node is not None
    return dict(node.state)


def _supervisor_status(ledger: LedgerService, name: str) -> object:
    record = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key=f"run-{name}:work"
    )
    assert record is not None
    return record.state["status"]


def _kill_and_reap(identity: ProcessIdentity) -> None:
    os.killpg(identity.process_group_id, signal.SIGKILL)
    os.waitpid(identity.pid, 0)


class _FireOnce:
    """A fault hook that raises exactly once at one named boundary."""

    def __init__(self, point: str) -> None:
        self.point = point
        self.fired = False

    def __call__(self, point: str) -> None:
        if point == self.point and not self.fired:
            self.fired = True
            raise RuntimeError(f"injected fault at {point}")


@pytest.mark.parametrize("point", ["termination_requested", "process_exit_observed"])
def test_an_interrupted_cancel_is_retried_to_a_single_bounded_cancellation(
    tmp_path: Path, ledger_service: LedgerService, point: str
) -> None:
    hook = _FireOnce(point)
    runtime = CoordinatorRuntime(ledger_service, owner="operator", fault_hook=hook)
    (running,) = _tick(runtime, _T0, _node(tmp_path, "work"))
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)

    with pytest.raises(RuntimeError, match=point):
        runtime.cancel_node(
            run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
        )
    assert hook.fired
    assert _state(ledger_service, "work")["status"] == "running"
    assert _supervisor_status(ledger_service, "work") == "termination_requested"

    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=3)
    )

    assert _state(ledger_service, "work")["status"] == "cancelled"
    assert probe_process(running.identity.pid) is None
    assert _supervisor_status(ledger_service, "work") == "exit_reconciled"
    assert _events(ledger_service, "worker.termination_requested", "work") == 1
    assert _events(ledger_service, "worker.cancelled", "work") == 1
    attempt = ledger_service.read_projection(
        aggregate_type="node_attempt", aggregate_id=running.lease.attempt_id
    )
    assert attempt is not None
    results = ledger_service.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'node_attempt.result_ingested' "
        "AND aggregate_id = ?",
        (running.lease.attempt_id,),
    ).fetchone()
    assert int(results[0]) == 1


def test_a_dead_worker_is_failed_with_evidence_and_never_launched_twice(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    (running,) = _tick(runtime, _T0, _node(tmp_path, "work"))
    queued = _node(tmp_path, "waiting")
    assert _tick(runtime, _T0 + timedelta(seconds=1), queued) == ()
    _kill_and_reap(running.identity)

    runtime.fail_exited_worker(
        run_id="run-work",
        node_id="work",
        now=_T0 + timedelta(seconds=2),
        heartbeat_window=_WINDOW,
    )

    failed = _state(ledger_service, "work")
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "worker_exited_without_result"
    evidence = failed["worker_exit_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["pid"] == running.identity.pid
    assert evidence["start_identity"] == running.identity.start_identity
    assert _supervisor_status(ledger_service, "work") == "exit_reconciled"
    # The slot is free: the queued run launches, the dead one is never launched again.
    started = _tick(runtime, _T0 + timedelta(seconds=3))
    assert [fixture.lease.run_id for fixture in started] == ["run-waiting"]
    assert _tick(runtime, _T0 + timedelta(seconds=4)) == ()
    assert _events(ledger_service, "worker.launch_intended", "work") == 1
    assert _events(ledger_service, "worker.launch_intended", "waiting") == 1
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=5), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-waiting", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=6)
    )


def test_a_worker_that_is_not_provably_dead_is_never_failed(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    (running,) = _tick(runtime, _T0, _node(tmp_path, "work"))

    with pytest.raises(ExternalConflictError, match="not provably gone"):
        runtime.fail_exited_worker(
            run_id="run-work",
            node_id="work",
            now=_T0 + timedelta(seconds=1),
            heartbeat_window=_WINDOW,
        )

    assert _state(ledger_service, "work")["status"] == "running"
    assert probe_process(running.identity.pid) == running.identity
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=2), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=3)
    )


def test_the_lease_epoch_is_active_only_while_the_launching_epoch_is(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    launcher = CoordinatorRuntime(ledger_service, owner="operator")
    (running,) = _tick(launcher, _T0, _node(tmp_path, "work"))
    assert running.lease.epoch == 1
    assert launcher.lease_epoch_active(run_id="run-work", node_id="work", now=_T0) is True

    # The launching epoch lapses and a newer coordinator takes over: the ledger has an active
    # owner again, but it is not the epoch that issued this worker's lease.
    later = _T0 + timedelta(seconds=120)
    successor = CoordinatorRuntime(ledger_service, owner="operator")
    assert successor.claim_epoch(now=later, heartbeat_window=_WINDOW).epoch == 2

    assert successor.coordinator.current_epoch() is not None
    assert (
        successor.lease_epoch_active(
            run_id="run-work", node_id="work", now=later + timedelta(seconds=1)
        )
        is False
    )
    assert successor.lease_epoch_active(run_id="run-none", node_id="work", now=later) is None
    successor.cancel_node(
        run_id="run-work",
        node_id="work",
        epoch=2,
        now=later + timedelta(seconds=2),
    )


_LEADER_WITH_GRANDCHILD = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    if sys.argv[2] == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"]
    )
    with open(sys.argv[1], "w") as handle:
        handle.write(str(child.pid))
    if sys.argv[2] == "exit-early":
        raise SystemExit(0)
    time.sleep(120)
    """
)


def _spawn_group(tmp_path: Path, mode: str) -> tuple[subprocess.Popen[bytes], ProcessIdentity, int]:
    marker = tmp_path / f"grandchild-{mode}"
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_WITH_GRANDCHILD, str(marker), mode],
        start_new_session=True,
    )
    # Identify the leader while it is certainly alive; one that exits early is gone later.
    identity = probe_process(leader.pid)
    assert identity is not None
    deadline = time.monotonic() + 15
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline, "the leader never started its grandchild"
        time.sleep(0.02)
    return leader, identity, int(marker.read_text())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_sigterm_ignoring_leader_and_its_grandchild_are_killed_as_a_whole_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_TERMINATION_GRACE_SECONDS", 0.5)
    leader, identity, grandchild = _spawn_group(tmp_path, "ignore-term")
    try:
        supervisor_module._terminate_exact_group(identity)

        assert leader.poll() is not None
        assert not _alive(grandchild)
        with pytest.raises(ProcessLookupError):
            os.killpg(identity.process_group_id, 0)
    finally:
        with pytest.raises(ProcessLookupError):
            os.killpg(identity.process_group_id, signal.SIGKILL)


def test_a_group_whose_leader_already_exited_but_whose_descendant_runs_is_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_TERMINATION_GRACE_SECONDS", 0.5)
    leader, identity, grandchild = _spawn_group(tmp_path, "exit-early")
    leader.wait(timeout=15)
    assert probe_process(identity.pid) is None
    assert _alive(grandchild)

    supervisor_module._terminate_exact_group(identity)

    assert not _alive(grandchild)


def test_a_group_that_survives_both_signals_is_reported_not_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_TERMINATION_GRACE_SECONDS", 0.2)
    leader, identity, grandchild = _spawn_group(tmp_path, "ignore-term")
    monkeypatch.setattr(os, "killpg", lambda *_: None)
    try:
        with pytest.raises(InternalInvariantViolationError, match="remains present"):
            supervisor_module._terminate_exact_group(identity)
    finally:
        monkeypatch.undo()
        os.killpg(identity.process_group_id, signal.SIGKILL)
        leader.wait(timeout=15)
        assert not _alive(grandchild)


def test_worker_stderr_goes_to_a_private_per_attempt_log_and_stdout_is_discarded(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    logs = tmp_path / "logs"
    program = (
        "import sys, time; "
        "sys.stderr.write('wrapper crash trace\\n'); sys.stderr.flush(); "
        "print('stdout noise'); time.sleep(120)"
    )
    node = _node(tmp_path, "work", command=(sys.executable, "-c", program), log_directory=logs)
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    (running,) = _tick(runtime, _T0, node)
    log = logs / "run-work-work-attempt-work.stderr.log"
    deadline = time.monotonic() + 15
    while not log.exists() or not log.read_text():
        assert time.monotonic() < deadline, "the worker's stderr was never captured"
        time.sleep(0.02)

    assert log.read_text() == "wrapper crash trace\n"
    assert (log.stat().st_mode & 0o777) == 0o600
    assert list(logs.iterdir()) == [log]
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
    )
    assert probe_process(running.identity.pid) is None


def test_termination_waits_for_a_term_ignoring_descendant_after_the_leader_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_TERMINATION_GRACE_SECONDS", 0.5)
    # The leader dies on SIGTERM but its descendant ignores it: the group is not gone yet.
    leader, identity, grandchild = _spawn_group(tmp_path, "plain")

    supervisor_module._terminate_exact_group(identity)

    assert leader.poll() is not None
    assert not _alive(grandchild)


@pytest.mark.parametrize("point", ["workspace_cleanup_started", "workspace_cleanup_recorded"])
def test_a_cancel_interrupted_inside_workspace_cleanup_completes_on_retry(
    tmp_path: Path, ledger_service: LedgerService, point: str
) -> None:
    hook = _FireOnce(point)
    runtime = CoordinatorRuntime(ledger_service, owner="operator", fault_hook=hook)
    node = _node(tmp_path, "work")
    (running,) = _tick(runtime, _T0, node)
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)

    with pytest.raises(RuntimeError, match=point):
        runtime.cancel_node(
            run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
        )
    assert hook.fired
    assert _state(ledger_service, "work")["status"] == "running"
    assert probe_process(running.identity.pid) is None

    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=3)
    )

    assert _state(ledger_service, "work")["status"] == "cancelled"
    reservation = runtime.read_workspace_reservation("repository-work")
    assert reservation is not None and reservation.status == "cleaned"
    assert not node.workspace_path.exists()
    results = ledger_service.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'node_attempt.result_ingested' "
        "AND aggregate_id = ?",
        (running.lease.attempt_id,),
    ).fetchone()
    assert int(results[0]) == 1
    assert _events(ledger_service, "worker.cancelled", "work") == 1
    # Repeating the cancel after completion is refused, not silently repeated.
    with pytest.raises(ExternalConflictError, match="only queued, running"):
        runtime.cancel_node(
            run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=4)
        )


def test_a_cancel_retry_completes_when_the_interrupted_cleanup_already_removed_the_directory(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    import shutil

    hook = _FireOnce("workspace_cleanup_started")
    runtime = CoordinatorRuntime(ledger_service, owner="operator", fault_hook=hook)
    node = _node(tmp_path, "work")
    _tick(runtime, _T0, node)
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)
    with pytest.raises(RuntimeError, match="workspace_cleanup_started"):
        runtime.cancel_node(
            run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
        )
    # The interrupted cleanup got as far as deleting the directory; git still lists it.
    shutil.rmtree(node.workspace_path)

    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=3)
    )

    assert _state(ledger_service, "work")["status"] == "cancelled"
    reservation = runtime.read_workspace_reservation("repository-work")
    assert reservation is not None and reservation.status == "cleaned"
    listed = _git("worktree", "list", "--porcelain", cwd=node.repository_path)
    assert str(node.workspace_path) not in listed


_TZ_LAUNCH = textwrap.dedent(
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

_TZ_ASSESS = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from enginery.engine.recovery import assess_worker
    from enginery.ledger.service import LedgerService

    ledger = LedgerService.open(Path(sys.argv[1]))
    record = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-work:work"
    )
    print(assess_worker(record.state).liveness.value)
    """
)

_TZ_CANCEL = textwrap.dedent(
    """
    import sys
    from datetime import UTC, datetime, timedelta
    from pathlib import Path
    from enginery.engine.runtime import CoordinatorRuntime
    from enginery.ledger.service import LedgerService

    ledger = LedgerService.open(Path(sys.argv[1]))
    runtime = CoordinatorRuntime(ledger, owner="operator")
    now = datetime.now(UTC)
    epoch = runtime.claim_epoch(now=now, heartbeat_window=timedelta(seconds=60))
    runtime.cancel_node(run_id="run-work", node_id="work", epoch=epoch.epoch, now=now)
    print("cancelled")
    """
)


def _python(code: str, *arguments: str, **environment: str | None) -> str:
    env = dict(os.environ)
    for name, value in environment.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return subprocess.run(
        [sys.executable, "-c", code, *arguments],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def test_worker_identity_is_alive_and_cancellable_across_time_zones_and_locales(
    tmp_path: Path, ledger_path: Path
) -> None:
    node = _node(tmp_path, "work")
    launched = _python(
        _TZ_LAUNCH,
        str(ledger_path),
        str(node.repository_path),
        str(node.workspace_path),
        node.base_revision,
        TZ=None,
        LANG=None,
        LC_ALL=None,
    )
    pid = int(launched)
    try:
        for assessing in (
            {"TZ": "UTC"},
            {"TZ": "Asia/Tokyo"},
            {"TZ": None, "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"},
            {"TZ": "Pacific/Kiritimati", "LANG": None},
        ):
            assert _python(_TZ_ASSESS, str(ledger_path), **assessing) == "alive", assessing
        assert probe_process(pid) is not None

        assert _python(_TZ_CANCEL, str(ledger_path), TZ="Asia/Tokyo") == "cancelled"

        assert probe_process(pid) is None
        ledger = LedgerService.open(ledger_path)
        try:
            assert _state(ledger, "work")["status"] == "cancelled"
            assert _supervisor_status(ledger, "work") == "exit_reconciled"
            assert _events(ledger, "worker.cancelled", "work") == 1
        finally:
            ledger.close()
    finally:
        if probe_process(pid) is not None:
            os.killpg(pid, signal.SIGKILL)


def test_a_legacy_unversioned_record_keeps_the_inherited_environment_comparison(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    (running,) = _tick(runtime, _T0, _node(tmp_path, "work"))
    inherited = probe_process(running.identity.pid, inherited_environment=True)
    assert inherited is not None
    legacy = {
        "run_id": "run-work",
        "node_id": "work",
        "attempt_id": "attempt-work",
        "pid": running.identity.pid,
        "process_group_id": running.identity.process_group_id,
        "start_identity": inherited.start_identity,
        "status": "running",
    }

    from enginery.engine.recovery import WorkerLiveness, assess_worker

    assert assess_worker(legacy).liveness is WorkerLiveness.ALIVE
    # Compared as a new record it would not match wherever the local time zone is not UTC0.
    assert probe_process(running.identity.pid, inherited_environment=False) == running.identity
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
    )


def test_observing_an_exit_twice_after_reconciliation_records_one_exit_event(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    node = _node(tmp_path, "work")
    (running,) = _tick(runtime, _T0, node)
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)
    supervisor = supervisor_module.WorkerSupervisor(ledger_service, runtime.coordinator)
    _kill_and_reap(running.identity)

    supervisor.observe_exit(
        lease=running.lease, coordinator_epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
    )
    from enginery.engine.recovery import RecoveryCoordinator

    reconciled = RecoveryCoordinator(ledger_service, runtime.coordinator).reconcile(
        lease=running.lease,
        workspace_path=node.workspace_path,
        epoch=epoch.epoch,
        now=_T0 + timedelta(seconds=3),
    )
    assert reconciled.ready_to_release
    assert _supervisor_status(ledger_service, "work") == "exit_reconciled"
    supervisor.observe_exit(
        lease=running.lease, coordinator_epoch=epoch.epoch, now=_T0 + timedelta(seconds=4)
    )
    supervisor.observe_exit(
        lease=running.lease, coordinator_epoch=epoch.epoch, now=_T0 + timedelta(seconds=5)
    )

    assert _events(ledger_service, "worker.process_exit_observed", "work") == 1
    assert _supervisor_status(ledger_service, "work") == "exit_reconciled"


@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal pid 1")
def test_cancel_on_a_pid_owned_by_another_user_records_the_mismatch_without_signalling(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    (running,) = _tick(runtime, _T0, _node(tmp_path, "work"))
    _kill_and_reap(running.identity)
    # The recorded worker is gone and its pid now belongs to init, which this user cannot signal.
    record = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-work:work"
    )
    assert record is not None
    state = dict(record.state)
    state.update(pid=1, process_group_id=1)
    ledger_service.connection.execute(
        "UPDATE process_manager_state SET state_json = ? "
        "WHERE process_manager_name = 'worker-supervisor' AND state_key = 'run-work:work'",
        (json.dumps(state),),
    )
    ledger_service.connection.commit()
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW)

    runtime.cancel_node(
        run_id="run-work", node_id="work", epoch=epoch.epoch, now=_T0 + timedelta(seconds=2)
    )

    assert _state(ledger_service, "work")["status"] == "cancelled"
    exit_state = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-work:work"
    )
    assert exit_state is not None
    assert exit_state.state["exit_reason"] == "start_identity_mismatch"
