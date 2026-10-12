"""Durable readiness: queued nodes are re-derived from the ledger on every tick.

Each scenario uses a real SQLite ledger, real git worktrees and real worker subprocesses.
No in-memory scheduler state is carried between the "before" and "after" coordinators.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.domain.errors import ExternalConflictError
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import CoordinatorRuntime, DispatchedFixture, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService

_WINDOW = timedelta(seconds=60)
_LEASE = timedelta(seconds=600)
_ONE_SLOT = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


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


def _node(
    tmp_path: Path,
    name: str,
    *,
    command: tuple[str, ...],
    dependencies: tuple[tuple[str, str], ...] = (),
) -> FixtureDispatch:
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
        dependencies=dependencies,
    )


_SLEEP = (sys.executable, "-c", "import time; time.sleep(60)")
_EXIT = (sys.executable, "-c", "pass")


def _until_released(marker: Path) -> tuple[str, ...]:
    """A real worker that holds its slot until the marker file appears, then exits."""
    program = f"import os, time\nwhile not os.path.exists({str(marker)!r}):\n    time.sleep(0.02)\n"
    return (sys.executable, "-c", program)


def _tick(
    runtime: CoordinatorRuntime,
    now: datetime,
    *requests: FixtureDispatch,
    limits: SchedulingLimits = _ONE_SLOT,
) -> tuple[DispatchedFixture, ...]:
    return runtime.tick(
        now=now,
        heartbeat_window=_WINDOW,
        lease_window=_LEASE,
        limits=limits,
        requests=requests,
    ).dispatched


def _state(ledger: LedgerService, run_id: str) -> dict[str, object]:
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id=f"run-{run_id}:work")
    assert node is not None
    return dict(node.state)


def _wakeup(ledger: LedgerService, run_id: str) -> dict[str, object]:
    wakeup = _state(ledger, run_id)["wakeup"]
    assert isinstance(wakeup, dict)
    return wakeup


def _launches(ledger: LedgerService, run_id: str) -> int:
    row = ledger.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'worker.launch_intended' "
        "AND aggregate_id = ?",
        (f"run-{run_id}:work",),
    ).fetchone()
    return int(row[0])


def _complete(runtime: CoordinatorRuntime, dispatched: DispatchedFixture, now: datetime) -> None:
    lease = dispatched.lease
    envelope = WorkerResultEnvelope(
        run_id=lease.run_id,
        node_id=lease.node_id,
        attempt_id=lease.attempt_id,
        epoch=lease.epoch,
        fencing_token=lease.fencing_token,
        operation_id=lease.operation_id,
        terminal_result="passed",
        artifact_references=(),
        result={},
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            runtime.ingest_result(envelope=envelope, now=now)
            return
        except ExternalConflictError as error:
            if "before supervised process exit" not in str(error) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_a_deferred_node_records_why_and_is_dispatched_by_a_restarted_coordinator(
    tmp_path: Path, ledger_path: Path
) -> None:
    release = tmp_path / "release-occupier"
    occupier = _node(tmp_path, "occupier", command=_until_released(release))
    queued = _node(tmp_path, "queued", command=_SLEEP)
    ledger = LedgerService.open(ledger_path)
    try:
        first = CoordinatorRuntime(ledger, owner="first")
        (running,) = _tick(first, _T0, occupier)
        assert _tick(first, _T0 + timedelta(seconds=1), queued) == ()
        deferred = _wakeup(ledger, "queued")
        assert deferred["reason"] == "global_capacity"
        assert deferred["evaluations"] == 1
        assert deferred["queued_since"] == (_T0 + timedelta(seconds=1)).isoformat()
        assert deferred["last_evaluated_epoch"] == 1

        # A tick with no new request re-evaluates from the ledger and counts the attempt.
        assert _tick(first, _T0 + timedelta(seconds=2)) == ()
        assert _wakeup(ledger, "queued")["evaluations"] == 2
    finally:
        ledger.close()

    # A different process: nothing but the ledger file survives. Its heartbeat window has
    # lapsed, so a replacement owner takes a newer epoch and the intent is still queued.
    restarted_at = _T0 + timedelta(seconds=120)
    ledger = LedgerService.open(ledger_path)
    try:
        replacement = CoordinatorRuntime(ledger, owner="replacement")
        assert _state(ledger, "queued")["status"] == "queued"
        assert _tick(replacement, restarted_at) == ()
        assert _wakeup(ledger, "queued")["evaluations"] == 3
        assert _wakeup(ledger, "queued")["last_evaluated_epoch"] == 2

        # Release the slot the way a worker really does -- it exits and its result is
        # ingested -- then the very next evaluation dispatches the queued node once.
        release.write_text("done", encoding="utf-8")
        _complete(replacement, running, restarted_at + timedelta(seconds=2))
        dispatched = _tick(replacement, restarted_at + timedelta(seconds=3))
        assert [item.lease.run_id for item in dispatched] == ["run-queued"]
        assert _tick(replacement, restarted_at + timedelta(seconds=4)) == ()
        assert _launches(ledger, "queued") == 1
        started = _state(ledger, "queued")
        assert started["status"] == "running"
        closed = _wakeup(ledger, "queued")
        assert closed["reason"] is None
        assert closed["evaluations"] == 3
        assert closed["dispatched_epoch"] == 2
        replacement.cancel_node(
            run_id="run-queued",
            node_id="work",
            epoch=dispatched[0].lease.epoch,
            now=restarted_at + timedelta(seconds=5),
        )
    finally:
        ledger.close()


def test_dependency_completion_redispatches_the_waiting_node_once(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    prerequisite = _node(tmp_path, "prerequisite", command=_EXIT)
    dependent = _node(
        tmp_path,
        "dependent",
        command=_SLEEP,
        dependencies=(("run-prerequisite", "work"),),
    )
    two_slots = SchedulingLimits(global_concurrency=2, per_repository_concurrency=2)
    runtime = CoordinatorRuntime(ledger_service, owner="operator")

    (running,) = _tick(runtime, _T0, prerequisite, limits=two_slots)
    assert _tick(runtime, _T0 + timedelta(seconds=1), dependent, limits=two_slots) == ()
    assert _wakeup(ledger_service, "dependent")["reason"] == "dependencies"

    _complete(runtime, running, _T0 + timedelta(seconds=2))

    dispatched = _tick(runtime, _T0 + timedelta(seconds=3), limits=two_slots)
    assert [item.lease.run_id for item in dispatched] == ["run-dependent"]
    assert _tick(runtime, _T0 + timedelta(seconds=4), limits=two_slots) == ()
    assert _launches(ledger_service, "dependent") == 1
    assert _wakeup(ledger_service, "dependent")["reason"] is None
    runtime.cancel_node(
        run_id="run-dependent",
        node_id="work",
        epoch=dispatched[0].lease.epoch,
        now=_T0 + timedelta(seconds=5),
    )


def test_a_node_waiting_on_a_cancelled_dependency_reports_it_instead_of_waiting_silently(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    prerequisite = _node(tmp_path, "prerequisite", command=_SLEEP)
    dependent = _node(
        tmp_path,
        "dependent",
        command=_SLEEP,
        dependencies=(("run-prerequisite", "work"),),
    )
    two_slots = SchedulingLimits(global_concurrency=2, per_repository_concurrency=2)
    runtime = CoordinatorRuntime(ledger_service, owner="operator")
    _tick(runtime, _T0, prerequisite, limits=two_slots)
    _tick(runtime, _T0 + timedelta(seconds=1), dependent, limits=two_slots)
    epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=2), heartbeat_window=_WINDOW)
    runtime.cancel_node(
        run_id="run-prerequisite",
        node_id="work",
        epoch=epoch.epoch,
        now=_T0 + timedelta(seconds=3),
    )

    assert _tick(runtime, _T0 + timedelta(seconds=4), limits=two_slots) == ()

    assert _wakeup(ledger_service, "dependent")["reason"] == "dependency_failed"
    assert _state(ledger_service, "dependent")["status"] == "queued"
    assert _launches(ledger_service, "dependent") == 0


def test_the_oldest_queued_run_wins_across_separate_coordinator_invocations(
    tmp_path: Path, ledger_path: Path
) -> None:
    occupier = _node(tmp_path, "occupier", command=_SLEEP)
    older = _node(tmp_path, "zzz", command=_SLEEP)  # queued first, but the higher run id
    newer = _node(tmp_path, "aaa", command=_SLEEP)  # queued later, but the lower run id

    def invocation(
        now: datetime, *requests: FixtureDispatch, runs: tuple[str, ...] | None = None
    ) -> tuple[DispatchedFixture, ...]:
        # A fresh process: a freshly opened ledger and a fresh coordinator, no shared memory.
        ledger = LedgerService.open(ledger_path)
        try:
            return (
                CoordinatorRuntime(ledger, owner="operator")
                .tick(
                    now=now,
                    heartbeat_window=_WINDOW,
                    lease_window=_LEASE,
                    limits=_ONE_SLOT,
                    requests=requests,
                    run_ids=runs,
                )
                .dispatched
            )
        finally:
            ledger.close()

    (held,) = invocation(_T0, occupier)
    assert invocation(_T0 + timedelta(seconds=1), older, runs=("run-zzz",)) == ()
    assert invocation(_T0 + timedelta(seconds=2), newer, runs=("run-aaa",)) == ()
    ledger = LedgerService.open(ledger_path)
    try:
        runtime = CoordinatorRuntime(ledger, owner="operator")
        epoch = runtime.claim_epoch(now=_T0 + timedelta(seconds=3), heartbeat_window=_WINDOW)
        runtime.cancel_node(
            run_id="run-occupier",
            node_id="work",
            epoch=epoch.epoch,
            now=_T0 + timedelta(seconds=4),
        )
    finally:
        ledger.close()
    assert held.lease.run_id == "run-occupier"

    # The lower run id advances first, but the older queued run keeps the freed slot.
    assert invocation(_T0 + timedelta(seconds=5), runs=("run-aaa",)) == ()
    ledger = LedgerService.open(ledger_path)
    try:
        reservation = _wakeup(ledger, "aaa")
        assert reservation["reason"] == "capacity_reserved_for_other_run"
        assert reservation["reserved_for"] == "run-zzz"
        assert _launches(ledger, "aaa") == 0
    finally:
        ledger.close()

    winner = invocation(_T0 + timedelta(seconds=6), runs=("run-zzz",))
    assert [fixture.lease.run_id for fixture in winner] == ["run-zzz"]
    ledger = LedgerService.open(ledger_path)
    try:
        assert _state(ledger, "aaa")["status"] == "queued"
        runtime = CoordinatorRuntime(ledger, owner="operator")
        runtime.cancel_node(
            run_id="run-zzz",
            node_id="work",
            epoch=runtime.claim_epoch(
                now=_T0 + timedelta(seconds=7), heartbeat_window=_WINDOW
            ).epoch,
            now=_T0 + timedelta(seconds=8),
        )
    finally:
        ledger.close()
    runner_up = invocation(_T0 + timedelta(seconds=9), runs=("run-aaa",))
    assert [fixture.lease.run_id for fixture in runner_up] == ["run-aaa"]
    ledger = LedgerService.open(ledger_path)
    try:
        runtime = CoordinatorRuntime(ledger, owner="operator")
        runtime.cancel_node(
            run_id="run-aaa",
            node_id="work",
            epoch=runtime.claim_epoch(
                now=_T0 + timedelta(seconds=10), heartbeat_window=_WINDOW
            ).epoch,
            now=_T0 + timedelta(seconds=11),
        )
    finally:
        ledger.close()
