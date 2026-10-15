"""Real-execution lifecycle scenarios for queued work, restarts, stale epochs and identity.

Every scenario builds its own SQLite ledger and git repositories in a temporary directory and
drives the production runtime. Coordinators that must outlive a process boundary are real
child processes, and every worker is a real subprocess; nothing here replays a canned receipt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.application.git_environment import repository_isolated_environment
from enginery.domain.errors import ExternalConflictError, HumanActionRequiredError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLeases
from enginery.engine.recovery import WorkerLiveness, assess_worker
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import CoordinatorRuntime, DispatchedFixture, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.supervisor import WorkerSupervisor, identity_from_state, probe_process
from enginery.ledger.service import LedgerService
from fault_injection.framework import FaultScenario

_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=60)
_LEASE = timedelta(minutes=30)
_ONE_SLOT = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_SLEEP = [sys.executable, "-c", "import time; time.sleep(300)"]

_REGISTER_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from datetime import datetime, timedelta
    from pathlib import Path
    from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
    from enginery.engine.scheduler import SchedulingLimits
    from enginery.ledger.service import LedgerService

    ledger_path, now, *encoded = sys.argv[1:]
    requests = []
    for raw in encoded:
        fields = json.loads(raw)
        fields["command"] = tuple(fields["command"])
        for key in ("repository_path", "workspace_path"):
            fields[key] = Path(fields[key])
        requests.append(FixtureDispatch(**fields))
    ledger = LedgerService.open(Path(ledger_path))
    runtime = CoordinatorRuntime(ledger, owner="operator")
    tick = runtime.tick(
        now=datetime.fromisoformat(now), heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(minutes=30), limits=SchedulingLimits(1, 1),
        requests=tuple(requests),
    )
    print(json.dumps([item.identity.pid for item in tick.dispatched]))
    ledger.close()
    """
)

_STALE_SCRIPT = textwrap.dedent(
    """
    import json, sys, time
    from datetime import datetime, timedelta
    from pathlib import Path
    from enginery.domain.errors import ExternalConflictError
    from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, FixtureRuntime
    from enginery.ledger.service import LedgerService

    ledger_path, go, now, encoded = sys.argv[1:]
    fields = json.loads(encoded)
    fields["command"] = tuple(fields["command"])
    for key in ("repository_path", "workspace_path"):
        fields[key] = Path(fields[key])
    request = FixtureDispatch(**fields)
    ledger = LedgerService.open(Path(ledger_path))
    stale = CoordinatorRuntime(ledger, owner="operator")
    epoch = stale.claim_epoch(
        now=datetime.fromisoformat(now), heartbeat_window=timedelta(seconds=60)
    )
    print(json.dumps({"held_epoch": epoch.epoch}), flush=True)
    deadline = time.monotonic() + 60
    while not Path(go).exists():
        if time.monotonic() > deadline:
            raise SystemExit("successor never took over")
        time.sleep(0.02)
    late = datetime.fromisoformat(now) + timedelta(seconds=121)
    try:
        FixtureRuntime(ledger, stale.coordinator).dispatch(
            request=request, epoch=epoch.epoch, now=late, lease_window=timedelta(minutes=30),
        )
        print(json.dumps({"outcome": "DISPATCHED"}), flush=True)
    except ExternalConflictError as error:
        print(json.dumps({"outcome": "REFUSED", "message": str(error)}), flush=True)
    ledger.close()
    """
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=repository_isolated_environment(),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _node(root: Path, name: str, *, command: list[str]) -> FixtureDispatch:
    repository = root / f"repository-{name}"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "fault@example.invalid", cwd=repository)
    _git("config", "user.name", "Fault", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return FixtureDispatch(
        run_id=f"run-{name}",
        node_id="work",
        attempt_id=f"attempt-{name}",
        repository_id=f"repository-{name}",
        repository_path=repository,
        workspace_path=root / f"workspace-{name}",
        base_revision=_git("rev-parse", "HEAD", cwd=repository),
        command=tuple(command),
        expected_attempt_version=0,
        operation_id=f"operation-{name}",
    )


def _encode(node: FixtureDispatch) -> str:
    return json.dumps(
        {
            "run_id": node.run_id,
            "node_id": node.node_id,
            "attempt_id": node.attempt_id,
            "repository_id": node.repository_id,
            "repository_path": str(node.repository_path),
            "workspace_path": str(node.workspace_path),
            "base_revision": node.base_revision,
            "command": list(node.command),
            "expected_attempt_version": node.expected_attempt_version,
            "operation_id": node.operation_id,
        }
    )


def _child(root: Path, script: str, *arguments: str) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real coordinator process whose stdout is a file.

    A pipe would not reach EOF until every orphaned worker the child launched has exited.
    """
    output = root / f"child-{time.monotonic_ns()}.out"
    with output.open("wb") as stdout:
        process = subprocess.Popen(
            [sys.executable, "-c", script, *arguments], stdout=stdout, cwd=root
        )
    return process, output


def _lines(output: Path) -> list[object]:
    return [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line]


def _run_child(root: Path, script: str, *arguments: str) -> list[object]:
    process, output = _child(root, script, *arguments)
    if process.wait(timeout=120) != 0:
        raise AssertionError(f"child coordinator exited {process.returncode}")
    return _lines(output)


def _launches(ledger: LedgerService, name: str) -> int:
    row = ledger.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'worker.launch_intended' "
        "AND aggregate_id = ?",
        (f"run-{name}:work",),
    ).fetchone()
    return int(row[0])


def _status(ledger: LedgerService, name: str) -> object:
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id=f"run-{name}:work")
    assert node is not None, f"{name} has no runtime node"
    return node.state["status"]


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


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _stop_leftovers(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), 9)
        except (ProcessLookupError, PermissionError):
            continue


def _queued_redispatch_after_capacity_release() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = LedgerService.open(root / "ledger.db")
        pids: list[int] = []
        try:
            runtime = CoordinatorRuntime(ledger, owner="operator")
            occupier = _node(root, "occupier", command=_SLEEP)
            queued = _node(root, "queued", command=_SLEEP)
            (held,) = _tick(runtime, _T0, occupier)
            pids.append(held.identity.pid)
            _expect(_tick(runtime, _T0 + timedelta(seconds=1), queued) == (), "queued node ran")
            _expect(_launches(ledger, "queued") == 0, "queued node launched before release")
            epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=2), heartbeat_window=_WINDOW)
            runtime.cancel_node(
                run_id="run-occupier",
                node_id="work",
                epoch=epoch.epoch,
                now=_T0 + timedelta(seconds=3),
            )
            dispatched = _tick(runtime, _T0 + timedelta(seconds=4))
            _expect(
                [item.lease.run_id for item in dispatched] == ["run-queued"],
                "capacity release did not redispatch the queued node",
            )
            pids.append(dispatched[0].identity.pid)
            for offset in (5, 6, 7):
                _expect(
                    _tick(runtime, _T0 + timedelta(seconds=offset)) == (),
                    "a repeated tick launched again",
                )
            _expect(_launches(ledger, "queued") == 1, "queued node launched more than once")
            _expect(_status(ledger, "queued") == "running", "redispatched node is not running")
            runtime.cancel_node(
                run_id="run-queued",
                node_id="work",
                epoch=epoch.epoch,
                now=_T0 + timedelta(seconds=8),
            )
            _expect(probe_process(dispatched[0].identity.pid) is None, "worker left running")
        finally:
            _stop_leftovers(pids)
            ledger.close()


def _restart_with_queued_work() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        occupier = _node(root, "occupier", command=_SLEEP)
        queued = _node(root, "queued", command=_SLEEP)
        ledger_path = root / "ledger.db"
        first = _run_child(
            root,
            _REGISTER_SCRIPT,
            str(ledger_path),
            _T0.isoformat(),
            _encode(occupier),
            _encode(queued),
        )
        occupier_pid = int(first[0][0])  # type: ignore[index]
        pids = [occupier_pid]
        ledger = LedgerService.open(ledger_path)
        try:
            _expect(_status(ledger, "queued") == "queued", "queued intent was not persisted")
            restarted = CoordinatorRuntime(ledger, owner="operator")
            later = _T0 + timedelta(seconds=120)
            epoch = restarted.claim_epoch(now=later, heartbeat_window=_WINDOW)
            _expect(epoch.epoch == 2, "restart did not take a newer epoch")
            _expect(_tick(restarted, later + timedelta(seconds=1)) == (), "ran while saturated")
            _expect(_launches(ledger, "queued") == 0, "queued node launched while saturated")
            # The occupier was launched by a different, now-exited process; releasing it by
            # cancellation must work from this later coordinator epoch.
            restarted.cancel_node(
                run_id="run-occupier",
                node_id="work",
                epoch=epoch.epoch,
                now=later + timedelta(seconds=2),
            )
            _expect(probe_process(occupier_pid) is None, "occupier worker was not terminated")
            dispatched = _tick(restarted, later + timedelta(seconds=3))
            _expect(
                [item.lease.run_id for item in dispatched] == ["run-queued"],
                "restarted coordinator did not dispatch the retained queued node",
            )
            pids.append(dispatched[0].identity.pid)
            _expect(_launches(ledger, "queued") == 1, "queued node launched more than once")
            restarted.cancel_node(
                run_id="run-queued",
                node_id="work",
                epoch=epoch.epoch,
                now=later + timedelta(seconds=4),
            )
        finally:
            _stop_leftovers(pids)
            ledger.close()


def _stale_epoch_dispatch_refusal() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "ledger.db"
        exiting = _node(root, "exiting", command=[sys.executable, "-c", "pass"])
        pending = _node(root, "pending", command=_SLEEP)
        go = root / "go"
        stale, stale_output = _child(
            root,
            _STALE_SCRIPT,
            str(ledger_path),
            str(go),
            _T0.isoformat(),
            _encode(pending),
        )
        ledger = LedgerService.open(ledger_path)
        try:
            deadline = time.monotonic() + 60
            while not any("held_epoch" in str(line) for line in _lines(stale_output)):
                _expect(time.monotonic() < deadline, "stale coordinator never claimed epoch 1")
                time.sleep(0.02)
            successor = CoordinatorRuntime(ledger, owner="operator")
            # The stale process holds epoch 1 only until its heartbeat window lapses; the
            # successor's own tick happens after that and takes epoch 2.
            later = _T0 + timedelta(seconds=120)
            (first,) = _tick(successor, later, exiting)
            _expect(first.lease.epoch == 2, "successor did not own epoch 2")
            go.write_text("go", encoding="utf-8")
            _expect(stale.wait(timeout=60) == 0, "stale coordinator process failed")
            outcome = _lines(stale_output)[-1]
            assert isinstance(outcome, dict)
            _expect(outcome["outcome"] == "REFUSED", f"stale coordinator dispatched: {outcome}")
            _expect(
                "not the current owner" in outcome["message"],
                f"stale dispatch was refused for the wrong reason: {outcome['message']}",
            )
            _expect(
                ledger.read_lease(run_id="run-pending", node_id="work") is None,
                "stale coordinator obtained a lease",
            )
            _expect(_launches(ledger, "pending") == 0, "stale coordinator launched a worker")
            stale_coordinator = Coordinator(ledger, owner="operator")
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
            deadline = time.monotonic() + 30
            while True:
                try:
                    WorkerSupervisor(ledger, successor.coordinator).observe_exit(
                        lease=first.lease, coordinator_epoch=2, now=later + timedelta(seconds=2)
                    )
                    break
                except ExternalConflictError:
                    _expect(time.monotonic() < deadline, "worker exit was never observed")
                    time.sleep(0.05)
            attempt = ledger.read_projection(
                aggregate_type="node_attempt", aggregate_id=first.lease.attempt_id
            )
            _expect(attempt is not None, "attempt projection missing")
            assert attempt is not None

            try:
                FencedNodeLeases(ledger, stale_coordinator).ingest_result(
                    envelope=envelope,
                    coordinator_epoch=1,
                    now=later + timedelta(seconds=3),
                    expected_attempt_version=attempt.aggregate_version,
                )
            except ExternalConflictError as error:
                _expect("not the current owner" in str(error), f"wrong refusal: {error}")
            else:
                raise AssertionError("a stale epoch ingested a worker result")
        finally:
            if stale.poll() is None:
                stale.kill()
            ledger.close()


def _cancel_queued_and_running() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "ledger.db"
        running = _node(root, "running", command=_SLEEP)
        # Sorts after the running node, so the single slot is taken by the first one.
        queued = _node(root, "waiting", command=_SLEEP)
        first = _run_child(
            root,
            _REGISTER_SCRIPT,
            str(ledger_path),
            _T0.isoformat(),
            _encode(running),
            _encode(queued),
        )
        worker_pid = int(first[0][0])  # type: ignore[index]
        ledger = LedgerService.open(ledger_path)
        try:
            runtime = CoordinatorRuntime(ledger, owner="operator")
            epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=5), heartbeat_window=_WINDOW)
            _expect(_status(ledger, "waiting") == "queued", "node was not queued behind the worker")
            runtime.cancel_node(
                run_id="run-waiting",
                node_id="work",
                epoch=epoch.epoch,
                now=_T0 + timedelta(seconds=6),
            )
            _expect(_status(ledger, "waiting") == "cancelled", "queued node was not cancelled")
            _expect(
                ledger.read_lease(run_id="run-waiting", node_id="work") is None,
                "cancelling a queued node invented a lease",
            )
            runtime.cancel_node(
                run_id="run-running",
                node_id="work",
                epoch=epoch.epoch,
                now=_T0 + timedelta(seconds=7),
            )
            _expect(probe_process(worker_pid) is None, "running worker survived cancellation")
            _expect(_status(ledger, "running") == "cancelled", "running node was not cancelled")
            record = ledger.read_process_manager_state(
                process_manager_name="worker-supervisor", state_key="run-running:work"
            )
            _expect(record is not None, "supervisor evidence was lost")
            assert record is not None
            identity = identity_from_state(record.state)
            _expect(identity.pid == worker_pid and not identity.legacy, "identity not retained")
            _expect(
                assess_worker(record.state).liveness is WorkerLiveness.EXITED,
                "cancelled worker is not classified as exited",
            )
            _expect(_tick(runtime, _T0 + timedelta(seconds=8)) == (), "work ran after cancel")
        finally:
            _stop_leftovers([worker_pid])
            ledger.close()


def _host_boot_mismatch_hold() -> None:
    for difference in ("machine_id",):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = LedgerService.open(root / "ledger.db")
            pids: list[int] = []
            try:
                runtime = CoordinatorRuntime(ledger, owner="operator")
                node = _node(root, "worker", command=_SLEEP)
                (running,) = _tick(runtime, _T0, node)
                pids.append(running.identity.pid)
                record = ledger.read_process_manager_state(
                    process_manager_name="worker-supervisor", state_key="run-worker:work"
                )
                assert record is not None
                state = dict(record.state)
                state[difference] = f"another-{difference}"
                ledger.connection.execute(
                    "UPDATE process_manager_state SET state_json = ? "
                    "WHERE process_manager_name = 'worker-supervisor' AND state_key = ?",
                    (json.dumps(state), "run-worker:work"),
                )
                ledger.connection.commit()
                _expect(
                    assess_worker(state).liveness is WorkerLiveness.FOREIGN_HOST,
                    f"{difference} mismatch was not classified as a foreign host",
                )
                epoch = runtime.claim_epoch(
                    now=_T0 + timedelta(seconds=1), heartbeat_window=_WINDOW
                )
                try:
                    runtime.cancel_node(
                        run_id="run-worker",
                        node_id="work",
                        epoch=epoch.epoch,
                        now=_T0 + timedelta(seconds=2),
                    )
                except HumanActionRequiredError:
                    pass
                else:
                    raise AssertionError(f"a {difference} mismatch was cancelled automatically")
                _expect(
                    probe_process(running.identity.pid) == running.identity,
                    f"a {difference} mismatch caused a local process to be signalled",
                )
                _expect(_status(ledger, "worker") == "running", "evidence was rewritten")
            finally:
                _stop_leftovers(pids)
                ledger.close()


def _reboot_reconciled() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = LedgerService.open(root / "ledger.db")
        pids: list[int] = []
        try:
            runtime = CoordinatorRuntime(ledger, owner="operator")
            node = _node(root, "worker", command=_SLEEP)
            (running,) = _tick(runtime, _T0, node)
            pids.append(running.identity.pid)
            record = ledger.read_process_manager_state(
                process_manager_name="worker-supervisor", state_key="run-worker:work"
            )
            assert record is not None
            state = dict(record.state)
            state["boot_id"] = "an earlier boot"
            ledger.connection.execute(
                "UPDATE process_manager_state SET state_json = ? "
                "WHERE process_manager_name = 'worker-supervisor' AND state_key = ?",
                (json.dumps(state), "run-worker:work"),
            )
            ledger.connection.commit()
            _expect(
                assess_worker(state).liveness is WorkerLiveness.REBOOTED,
                "a different boot on the same machine was not classified as a reboot",
            )
            later = _T0 + timedelta(seconds=120)
            runtime.fail_exited_worker(
                run_id="run-worker", node_id="work", now=later, heartbeat_window=_WINDOW
            )
            _expect(_status(ledger, "worker") == "failed", "a rebooted worker was not failed")
            node_state = ledger.read_projection(
                aggregate_type="runtime_node", aggregate_id="run-worker:work"
            )
            _expect(node_state is not None, "node state is missing")
            assert node_state is not None
            _expect(
                node_state.state["failure_reason"] == "worker_lost_to_reboot",
                "the reboot was not recorded as its own failure reason",
            )
            _expect(
                probe_process(running.identity.pid) == running.identity,
                "a rebooted worker's pid was signalled",
            )
        finally:
            _stop_leftovers(pids)
            ledger.close()


def _worker_death_bounded_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger_path = root / "ledger.db"
        dead = _node(root, "dead", command=_SLEEP)
        waiting = _node(root, "waiting", command=_SLEEP)
        first = _run_child(
            root,
            _REGISTER_SCRIPT,
            str(ledger_path),
            _T0.isoformat(),
            _encode(dead),
            _encode(waiting),
        )
        worker_pid = int(first[0][0])  # type: ignore[index]
        ledger = LedgerService.open(ledger_path)
        pids = [worker_pid]
        try:
            os.killpg(os.getpgid(worker_pid), 9)
            deadline = time.monotonic() + 15
            while probe_process(worker_pid) is not None:
                _expect(time.monotonic() < deadline, "the killed worker never vanished")
                time.sleep(0.02)
            runtime = CoordinatorRuntime(ledger, owner="operator")
            later = _T0 + timedelta(seconds=120)
            runtime.fail_exited_worker(
                run_id="run-dead", node_id="work", now=later, heartbeat_window=_WINDOW
            )
            _expect(_status(ledger, "dead") == "failed", "the dead worker was not failed")
            record = ledger.read_process_manager_state(
                process_manager_name="worker-supervisor", state_key="run-dead:work"
            )
            _expect(record is not None, "supervisor evidence was lost")
            assert record is not None
            _expect(
                identity_from_state(record.state).pid == worker_pid,
                "the dead worker's identity was not retained",
            )
            started = _tick(runtime, later + timedelta(seconds=1))
            _expect(
                [item.lease.run_id for item in started] == ["run-waiting"],
                "the freed slot did not go to the queued run",
            )
            pids.append(started[0].identity.pid)
            _expect(_tick(runtime, later + timedelta(seconds=2)) == (), "a tick launched again")
            _expect(_launches(ledger, "dead") == 1, "the dead worker was launched again")
            _expect(_launches(ledger, "waiting") == 1, "the queued run launched more than once")
        finally:
            _stop_leftovers(pids)
            ledger.close()


def lifecycle_scenarios() -> tuple[FaultScenario, ...]:
    cases: tuple[tuple[str, str, Callable[[], None]], ...] = (
        (
            "queued_redispatch_after_capacity_release",
            "a queued node is dispatched once on the first tick after capacity is released",
            _queued_redispatch_after_capacity_release,
        ),
        (
            "restart_with_queued_work",
            "a restarted coordinator dispatches queued work launched by another process",
            _restart_with_queued_work,
        ),
        (
            "stale_epoch_dispatch_refusal",
            "a real stale coordinator process can neither dispatch nor ingest under an old epoch",
            _stale_epoch_dispatch_refusal,
        ),
        (
            "cancel_queued_and_running",
            "cancelling queued and running nodes preserves identity evidence and ends bounded",
            _cancel_queued_and_running,
        ),
        (
            "host_boot_mismatch_hold",
            "a worker record from another host or boot is held and never signalled",
            _host_boot_mismatch_hold,
        ),
        (
            "reboot_reconciled_not_signalled",
            "a worker recorded under an earlier boot is failed as lost to reboot, never signalled",
            _reboot_reconciled,
        ),
        (
            "worker_death_bounded_state",
            "a SIGKILLed worker is failed with identity evidence and frees its slot once",
            _worker_death_bounded_state,
        ),
    )
    return tuple(FaultScenario(name, description, run) for name, description, run in cases)


__all__ = ["lifecycle_scenarios"]
