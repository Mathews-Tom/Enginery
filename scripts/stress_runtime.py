#!/usr/bin/env python3
"""Run seeded, real-process stress against one durable SQLite ledger."""

from __future__ import annotations

import argparse
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.domain.errors import ExternalConflictError
from enginery.engine.coordinator import Coordinator
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import CoordinatorRuntime, DispatchedFixture, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.supervisor import WorkerSupervisor, probe_process
from enginery.ledger.service import LedgerService


@dataclass(slots=True)
class Counters:
    runs_requested: int = 0
    runs_started: int = 0
    runs_completed: int = 0
    runs_cancelled: int = 0
    leases_granted: int = 0
    workers_started: int = 0
    reservations: int = 0
    retained_workspaces: int = 0
    reconciliation_blocks: int = 0
    duplicate_leases: int = 0
    duplicate_process_groups: int = 0
    workspace_collisions: int = 0
    accepted_stale_results: int = 0
    active_human_wait_leases: int = 0
    orphaned_process_groups: int = 0

    def render(self) -> str:
        return "\n".join(f"{field}={getattr(self, field)}" for field in self.__dataclass_fields__)

    def safe(self) -> bool:
        return all(
            getattr(self, field) == 0
            for field in (
                "duplicate_leases",
                "duplicate_process_groups",
                "workspace_collisions",
                "accepted_stale_results",
                "active_human_wait_leases",
                "orphaned_process_groups",
            )
        )


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(root: Path, index: int) -> tuple[Path, str]:
    repository = root / f"repository-{index}"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "stress@example.invalid", cwd=repository)
    _git("config", "user.name", "Stress", cwd=repository)
    (repository / "README").write_text(f"{index}\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _exercise_epoch_race(database: Path, now: datetime) -> None:
    barrier = threading.Barrier(2)
    first_connection_opened = threading.Event()
    outcomes: list[str] = []

    def contender(owner: str, *, wait_for_first_connection: bool) -> None:
        if wait_for_first_connection:
            first_connection_opened.wait()
        ledger = LedgerService.open(database)
        if not wait_for_first_connection:
            first_connection_opened.set()
        try:
            barrier.wait()
            Coordinator(ledger, owner=owner).acquire(
                now=now,
                heartbeat_window=timedelta(seconds=30),
            )
            outcomes.append(owner)
        except (ExternalConflictError, sqlite3.OperationalError):
            outcomes.append("conflict")
        finally:
            ledger.close()

    first = threading.Thread(
        target=contender,
        args=("race-a",),
        kwargs={"wait_for_first_connection": False},
    )
    second = threading.Thread(
        target=contender,
        args=("race-b",),
        kwargs={"wait_for_first_connection": True},
    )
    first.start()
    second.start()
    first.join()
    second.join()
    if len(outcomes) != 2 or sum(result in {"race-a", "race-b"} for result in outcomes) != 1:
        raise RuntimeError(f"coordinator epoch race was not fenced: {outcomes}")


def _envelope(dispatched: DispatchedFixture) -> WorkerResultEnvelope:
    lease = dispatched.lease
    return WorkerResultEnvelope(
        run_id=lease.run_id,
        node_id=lease.node_id,
        attempt_id=lease.attempt_id,
        epoch=lease.epoch,
        fencing_token=lease.fencing_token,
        operation_id=lease.operation_id,
        terminal_result="cancelled",
        artifact_references=(),
        result={"stress": "cancelled"},
    )


def _finish_batch(
    *,
    ledger: LedgerService,
    runtime: CoordinatorRuntime,
    batch: tuple[DispatchedFixture, ...],
    now: datetime,
    counters: Counters,
) -> None:
    for dispatched in batch:
        supervisor = WorkerSupervisor(ledger, runtime.coordinator)
        supervisor.cancel(lease=dispatched.lease, identity=dispatched.identity, now=now)
        runtime.ingest_result(envelope=_envelope(dispatched), now=now)
        if probe_process(dispatched.identity.pid) is not None:
            counters.orphaned_process_groups += 1
        counters.runs_cancelled += 1
        counters.runs_completed += 1


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--global-concurrency", type=int, required=True)
    parser.add_argument("--same-repository-runs", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.runs < 1 or arguments.global_concurrency < 1 or arguments.same_repository_runs < 2:
        raise SystemExit("invalid run, concurrency, or same-repository-run count")
    return arguments


def main() -> int:
    arguments = _parse_arguments()
    randomizer = random.Random(arguments.seed)
    counters = Counters(runs_requested=arguments.runs)
    now = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "ledger.db"
        _exercise_epoch_race(database, now)
        now += timedelta(seconds=31)
        ledger = LedgerService.open(database)
        try:
            runtime = CoordinatorRuntime(ledger, owner="stress-coordinator")
            repositories = [
                _repository(root, index)
                for index in range(arguments.runs - arguments.same_repository_runs + 1)
            ]
            requests: list[FixtureDispatch] = []
            for index in range(arguments.runs):
                repository_index = (
                    0
                    if index < arguments.same_repository_runs
                    else index - arguments.same_repository_runs + 1
                )
                repository, revision = repositories[repository_index]
                requests.append(
                    FixtureDispatch(
                        run_id=f"run-{index}",
                        node_id="node",
                        attempt_id=f"attempt-{index}",
                        repository_id=f"repository-{repository_index}",
                        repository_path=repository,
                        workspace_path=root / f"workspace-{index}",
                        base_revision=revision,
                        command=(sys.executable, "-c", "import time; time.sleep(60)"),
                        expected_attempt_version=0,
                        operation_id=f"operation-{index}",
                    )
                )
            randomizer.shuffle(requests)
            limits = SchedulingLimits(
                global_concurrency=arguments.global_concurrency,
                per_repository_concurrency=1,
            )
            pending = tuple(requests)
            while counters.runs_completed < arguments.runs:
                tick = runtime.tick(
                    now=now,
                    heartbeat_window=timedelta(seconds=30),
                    lease_window=timedelta(seconds=20),
                    limits=limits,
                    requests=pending,
                )
                pending = ()
                if not tick.dispatched:
                    raise RuntimeError("scheduler made no progress with queued stress work")
                counters.runs_started += len(tick.dispatched)
                counters.leases_granted += len(tick.dispatched)
                counters.workers_started += len(tick.dispatched)
                counters.reservations += len(tick.dispatched)
                seen_groups = {fixture.identity.process_group_id for fixture in tick.dispatched}
                if len(seen_groups) != len(tick.dispatched):
                    counters.duplicate_process_groups += 1
                _finish_batch(
                    ledger=ledger,
                    runtime=runtime,
                    batch=tick.dispatched,
                    now=now,
                    counters=counters,
                )
                now += timedelta(seconds=1)
            if counters.runs_started != arguments.runs:
                raise RuntimeError("stress scheduler did not start every requested run")
        finally:
            ledger.close()
    print(counters.render())
    return 0 if counters.safe() else 1


if __name__ == "__main__":
    raise SystemExit(main())
