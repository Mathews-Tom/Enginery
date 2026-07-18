#!/usr/bin/env python3
"""Exercise every durable M5 worker lifecycle fault boundary."""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import CoordinatorRuntime, DispatchedFixture, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.supervisor import ProcessIdentity, WorkerSupervisor, probe_process
from enginery.ledger.service import LedgerService
from fault_injection.framework import FaultScenario, main_for

FAULT_POINTS = (
    "epoch_acquired",
    "workspace_reserved",
    "workspace_materialized",
    "lease_granted",
    "worker_launch_intended",
    "worker_process_started",
    "worker_identity_persisted",
    "result_received",
    "result_ingested",
    "termination_requested",
    "process_exit_observed",
    "workspace_cleanup_started",
    "workspace_cleanup_recorded",
    "human_wait_entered",
    "human_wait_resumed",
)


class InjectedFaultError(RuntimeError):
    pass


@dataclass(slots=True)
class FaultGate:
    point: str | None = None

    def trigger(self, point: str) -> None:
        if point == self.point:
            raise InjectedFaultError(point)


def _git(*args: str, cwd: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "fault@example.invalid", cwd=repository)
    _git("config", "user.name", "Fault", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _request(repository: Path, revision: str, workspace: Path, attempt: str) -> FixtureDispatch:
    return FixtureDispatch(
        run_id="run-1",
        node_id="node-1",
        attempt_id=attempt,
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=workspace,
        base_revision=revision,
        command=(sys.executable, "-c", "pass"),
        expected_attempt_version=0,
        operation_id=f"operation-{attempt}",
    )


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
        result={"fault": "matrix"},
    )


def _stop_for_result(
    ledger: LedgerService, runtime: CoordinatorRuntime, dispatched: DispatchedFixture, now: datetime
) -> None:
    WorkerSupervisor(ledger, runtime.coordinator).cancel(
        lease=dispatched.lease,
        identity=dispatched.identity,
        now=now,
    )


def _takeover_and_reconcile(ledger: LedgerService, now: datetime) -> None:
    replacement = Coordinator(ledger, owner="replacement")
    replacement.acquire(now=now + timedelta(seconds=61), heartbeat_window=timedelta(seconds=60))
    current = replacement.current_epoch()
    if current is None or current.owner != "replacement":
        raise AssertionError("replacement coordinator did not own a newer epoch")
    record = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor",
        state_key="run-1:node-1",
    )
    lease_record = ledger.read_lease(run_id="run-1", node_id="node-1")
    if record is None or lease_record is None:
        return
    state = record.state
    pid = state.get("pid")
    process_group_id = state.get("process_group_id")
    start_identity = state.get("start_identity")
    operation_id = state.get("operation_id")
    if not (
        isinstance(pid, int)
        and isinstance(process_group_id, int)
        and isinstance(start_identity, str)
        and isinstance(operation_id, str)
        and lease_record.expires_at is not None
    ):
        return
    lease = FencedNodeLease(
        run_id=lease_record.run_id,
        node_id=lease_record.node_id,
        attempt_id=lease_record.attempt_id,
        epoch=lease_record.epoch,
        fencing_token=lease_record.fencing_token,
        operation_id=operation_id,
        owner=lease_record.owner,
        expires_at=datetime.fromisoformat(lease_record.expires_at),
    )
    identity = ProcessIdentity(pid, process_group_id, start_identity)
    WorkerSupervisor(ledger, replacement).enforce_heartbeat(
        lease=lease,
        identity=identity,
        now=now + timedelta(seconds=61),
    )
    if probe_process(pid) is not None:
        raise AssertionError("replacement left an orphaned process group")


def _run_boundary(point: str) -> None:
    now = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = LedgerService.open(root / "ledger.db")
        try:
            repository, revision = _repository(root)
            gate = FaultGate(point if point == "epoch_acquired" else None)
            runtime = CoordinatorRuntime(ledger, owner="first", fault_hook=gate.trigger)
            request = _request(repository, revision, root / "workspace", "attempt-1")
            limits = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
            dispatched: DispatchedFixture | None = None
            try:
                if point == "epoch_acquired":
                    runtime.tick(
                        now=now,
                        heartbeat_window=timedelta(seconds=60),
                        lease_window=timedelta(seconds=30),
                        limits=limits,
                        requests=(request,),
                    )
                elif point in {
                    "workspace_reserved",
                    "workspace_materialized",
                    "lease_granted",
                    "worker_launch_intended",
                    "worker_process_started",
                    "worker_identity_persisted",
                }:
                    gate.point = point
                    runtime.tick(
                        now=now,
                        heartbeat_window=timedelta(seconds=60),
                        lease_window=timedelta(seconds=30),
                        limits=limits,
                        requests=(request,),
                    )
                else:
                    initial = runtime.tick(
                        now=now,
                        heartbeat_window=timedelta(seconds=60),
                        lease_window=timedelta(seconds=30),
                        limits=limits,
                        requests=(request,),
                    )
                    dispatched = initial.dispatched[0]
                    if point in {
                        "result_received",
                        "result_ingested",
                        "workspace_cleanup_started",
                        "workspace_cleanup_recorded",
                    }:
                        _stop_for_result(
                            ledger,
                            runtime,
                            dispatched,
                            now + timedelta(seconds=1),
                        )
                        gate.point = point
                        runtime.ingest_result(
                            envelope=_envelope(dispatched),
                            now=now + timedelta(seconds=1),
                        )
                    elif point in {
                        "termination_requested",
                        "process_exit_observed",
                        "human_wait_entered",
                    }:
                        gate.point = point
                        runtime.enter_human_wait(
                            dispatched=dispatched,
                            reason="fault injection",
                            now=now + timedelta(seconds=1),
                        )
                    else:
                        runtime.enter_human_wait(
                            dispatched=dispatched,
                            reason="fault injection",
                            now=now + timedelta(seconds=1),
                        )
                        gate.point = point
                        runtime.resume_human_wait(
                            request=_request(
                                repository,
                                revision,
                                root / "workspace",
                                "attempt-2",
                            ),
                            epoch=initial.epoch.epoch,
                            now=now + timedelta(seconds=2),
                        )
            except InjectedFaultError as error:
                if str(error) != point:
                    raise AssertionError(f"wrong fault boundary: {error}") from error
            else:
                raise AssertionError(f"fault boundary {point} did not interrupt execution")
            _takeover_and_reconcile(ledger, now)
        finally:
            ledger.close()


def _platform_supported() -> None:
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise AssertionError(f"unsupported platform {sys.platform}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True)
    parser.parse_args()
    scenarios = [
        FaultScenario(
            "platform_identity",
            "current platform supports process identity",
            _platform_supported,
        )
    ]
    scenarios.extend(
        FaultScenario(
            point, f"durable lifecycle boundary {point}", lambda point=point: _run_boundary(point)
        )
        for point in FAULT_POINTS
    )
    return main_for(tuple(scenarios))


if __name__ == "__main__":
    raise SystemExit(main())
