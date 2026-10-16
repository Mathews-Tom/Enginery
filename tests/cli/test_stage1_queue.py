"""``stage1 watch --advance`` redispatches durably queued implementation work.

The scenario runs the real CLI entry point against a real SQLite ledger, a real git
worktree and real worker subprocesses; the harness is a local stand-in executable that
records every launch.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from enginery.application.work_ports import PullRequestPort, WorkLedgerPort
from enginery.cli.main import main
from enginery.cli.stage1_composition import harness_for, worker_report
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.supervisor import probe_host
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1RunService, stage1_request_from_state
from tests.cli.test_stage1_execution_cli import _build_argv, _Run
from tests.workflows.test_stage1_runtime import RecordingPullRequests, TerminalWorkLedger

_WINDOW = timedelta(seconds=60)
_LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _occupier(tmp_path: Path) -> FixtureDispatch:
    repository = tmp_path / "occupier-repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return FixtureDispatch(
        run_id="run-occupier",
        node_id="hold",
        attempt_id="attempt-occupier",
        repository_id="repository-occupier",
        repository_path=repository,
        workspace_path=tmp_path / "workspace-occupier",
        base_revision=_git("rev-parse", "HEAD", cwd=repository),
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id="operation-occupier",
    )


def _watch(capsys: pytest.CaptureFixture[str], run: _Run, *, advance: bool) -> dict[str, object]:
    capsys.readouterr()
    argv = ["stage1", "watch", "--database", str(run.database), "--owner", "op"]
    argv += ["--run-id", "run-1"]
    if advance:
        argv.append("--advance")
    assert main(argv) == 0
    return cast(dict[str, object], json.loads(capsys.readouterr().out.strip().splitlines()[-1]))


def _implement(payload: dict[str, object]) -> dict[str, object]:
    nodes = cast(list[dict[str, object]], payload["nodes"])
    return next(node for node in nodes if node["node_id"] == "implement")


def test_watch_advance_dispatches_queued_work_once_after_capacity_is_released(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _Run(tmp_path, "omp")
    now = datetime.now(UTC)
    ledger = LedgerService.open(run.database)
    try:
        service, request = run.service(ledger)
        runtime = service.runtime
        assert (
            len(
                runtime.tick(
                    now=now,
                    heartbeat_window=_WINDOW,
                    lease_window=timedelta(minutes=10),
                    limits=_LIMITS,
                    requests=(_occupier(tmp_path),),
                ).dispatched
            )
            == 1
        )
        service.qualify(
            request,
            external_reference=request.work_snapshot.work_item.external_reference,
            applicable_criteria=request.applicable_criteria,
            now=now + timedelta(seconds=1),
            heartbeat_window=_WINDOW,
        )
        queued = service.dispatch_implementation(
            request,
            now=now + timedelta(seconds=2),
            heartbeat_window=_WINDOW,
            lease_window=timedelta(minutes=10),
            limits=_LIMITS,
        )
        assert queued.fixture is None
    finally:
        ledger.close()

    # The queued state is visible read-only: what is waiting, why, and how often it was seen.
    shown = _watch(capsys, run, advance=False)
    assert shown["next_action"] == "dispatch_queued"
    wakeup = cast(dict[str, object], _implement(shown)["wakeup"])
    assert _implement(shown)["status"] == "queued"
    assert wakeup["reason"] == "global_capacity"
    assert wakeup["evaluations"] == 1

    # Advancing while the slot is still taken re-evaluates, records it, and launches nothing.
    blocked = _watch(capsys, run, advance=True)
    assert blocked["action_taken"] == "dispatch_queued"
    assert blocked["next_action"] == "dispatch_queued"
    assert cast(dict[str, object], _implement(blocked)["wakeup"])["evaluations"] == 2
    assert run.launches() == []

    # Release the occupying slot, then the next advance dispatches the queued node.
    ledger = LedgerService.open(run.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner="op")
        epoch = runtime.claim_epoch(now=datetime.now(UTC), heartbeat_window=_WINDOW)
        runtime.cancel_node(
            run_id="run-occupier", node_id="hold", epoch=epoch.epoch, now=datetime.now(UTC)
        )
    finally:
        ledger.close()
    dispatched = _watch(capsys, run, advance=True)
    assert dispatched["action_taken"] == "dispatch_queued"
    assert dispatched["next_action"] in {"wait", "collect_implementation"}
    started = _implement(dispatched)
    assert started["status"] == "running"
    assert cast(dict[str, object], started["wakeup"])["reason"] is None

    # The stand-in is launched exactly once and further evaluation never launches another.
    deadline = time.monotonic() + 30
    while len(run.launches()) < 1:
        assert time.monotonic() < deadline, "the dispatched worker never launched the harness"
        time.sleep(0.02)
    after = _watch(capsys, run, advance=False)
    assert after["next_action"] in {"wait", "collect_implementation"}
    assert len(run.launches()) == 1


def test_two_runs_built_without_an_attempt_id_flag_can_both_dispatch_on_one_ledger(
    tmp_path: Path,
) -> None:
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    assert main(_build_argv(tmp_path, output=first_path)) == 0
    second = _occupier(tmp_path)  # a second repository with a commit, reused as run 2's
    assert (
        main(
            _build_argv(
                tmp_path,
                "--run-id",
                "run-2",
                "--repository",
                "owner/other",
                "--external-reference",
                "owner/other#1",
                "--repository-path",
                str(second.repository_path),
                "--base-revision",
                second.base_revision,
                "--workspace-path",
                str(tmp_path / "workspace-2"),
                "--artifact-root",
                str(tmp_path / "artifacts-2"),
                output=second_path,
            )
        )
        == 0
    )
    database = tmp_path / "shared.db"
    for path in (first_path, second_path):
        assert (
            main(
                [
                    "stage1",
                    "start",
                    "--database",
                    str(database),
                    "--owner",
                    "op",
                    "--request",
                    str(path),
                ]
            )
            == 0
        )
    first = stage1_request_from_state(json.loads(first_path.read_text()))
    other = stage1_request_from_state(json.loads(second_path.read_text()))
    assert first.implementation.attempt_id == "run-1-implement-0"
    assert other.implementation.attempt_id == "run-2-implement-0"

    ledger = LedgerService.open(database)
    try:
        runtime = CoordinatorRuntime(ledger, owner="op")
        for request in (first, other):
            service = Stage1RunService(
                runtime=runtime,
                ledger=ledger,
                work_ledger=cast(WorkLedgerPort, TerminalWorkLedger(request.work_snapshot)),
                pull_requests=cast(PullRequestPort, RecordingPullRequests()),
                harness=harness_for(request.execution_configuration),
            )
            now = datetime.now(UTC)
            service.qualify(
                request,
                external_reference=request.work_snapshot.work_item.external_reference,
                applicable_criteria=request.applicable_criteria,
                now=now,
                heartbeat_window=_WINDOW,
            )
            dispatched = service.dispatch_implementation(
                request,
                now=now,
                heartbeat_window=_WINDOW,
                lease_window=timedelta(minutes=10),
                limits=_LIMITS,
            )
            assert dispatched.fixture is not None
            assert dispatched.fixture.lease.attempt_id == request.implementation.attempt_id
            epoch = runtime.claim_epoch(now=now, heartbeat_window=_WINDOW)
            runtime.cancel_node(
                run_id=str(request.run.id), node_id="implement", epoch=epoch.epoch, now=now
            )
    finally:
        ledger.close()


def test_build_request_rejects_a_run_id_too_long_for_its_derived_attempt_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "long.json"
    long_run_id = "r" * 117  # "<run-id>-implement-0" is then 129 characters; the limit is 128

    code = main(_build_argv(tmp_path, "--run-id", long_run_id, output=output))

    assert code == 2
    assert not output.exists()
    assert (
        "run id is too long to derive a valid implementation attempt id" in capsys.readouterr().err
    )

    # The default id fits at 116, but the longest repair id does not once repairs are allowed.
    borderline = "r" * 116
    code = main(
        _build_argv(tmp_path, "--run-id", borderline, "--repair-limit", "10", output=output)
    )
    assert code == 2
    assert not output.exists()
    # An explicit first-attempt id does not exempt the repair ids, which are always derived
    # from the run id, and the message must not advise a flag that cannot fix that.
    capsys.readouterr()
    code = main(
        _build_argv(
            tmp_path,
            "--run-id",
            long_run_id,
            "--implementation-attempt-id",
            "short-attempt-0",
            output=output,
        )
    )
    assert code == 2
    assert not output.exists()
    message = capsys.readouterr().err
    assert "run id is too long" in message
    assert "--implementation-attempt-id" not in message
    # With no repair attempts possible the first id is the only one, and the flag does help.
    capsys.readouterr()
    code = main(
        _build_argv(tmp_path, "--run-id", long_run_id, "--repair-limit", "0", output=output)
    )
    assert code == 2
    assert "--implementation-attempt-id" in capsys.readouterr().err
    # With no repair attempts possible, an explicit id is the operator's choice.
    code = main(
        _build_argv(
            tmp_path,
            "--run-id",
            long_run_id,
            "--implementation-attempt-id",
            "short-attempt-0",
            "--repair-limit",
            "0",
            output=output,
        )
    )
    assert code == 0
    assert output.exists()


def test_a_legacy_run_whose_repair_id_would_overflow_fails_loudly_at_derivation() -> None:
    from enginery.domain.errors import InvalidInputError
    from enginery.workflows.stage1 import implementation_attempt_id

    assert implementation_attempt_id("r" * 116, 0) == f"{'r' * 116}-implement-0"
    with pytest.raises(InvalidInputError, match="too long to derive"):
        implementation_attempt_id("r" * 116, 10)


def test_worker_report_tells_a_live_worker_from_a_dead_one_with_identity_evidence(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="op")
    now = datetime.now(UTC)
    (running,) = runtime.tick(
        now=now,
        heartbeat_window=_WINDOW,
        lease_window=timedelta(minutes=10),
        limits=_LIMITS,
        requests=(_occupier(tmp_path),),
    ).dispatched

    alive = worker_report(ledger_service, runtime, run_id="run-occupier", node_id="hold")
    assert alive is not None
    assert alive["liveness"] == "alive"
    assert alive["pid"] == running.identity.pid
    assert alive["start_identity"] == running.identity.start_identity
    assert alive["identity_version"] == 2
    assert alive["host"] == {
        "machine_id": probe_host().machine_id,
        "boot_id": probe_host().boot_id,
        "hostname": probe_host().hostname,
    }
    assert (alive["lease_epoch"], alive["coordinator_epoch"]) == (1, 1)
    assert alive["lease_epoch_active"] is True

    os.killpg(running.identity.process_group_id, signal.SIGKILL)
    os.waitpid(running.identity.pid, 0)

    dead = worker_report(ledger_service, runtime, run_id="run-occupier", node_id="hold")
    assert dead is not None
    assert dead["liveness"] == "exited"
    assert dead["lease_epoch_active"] is True
    assert dead["pid"] == running.identity.pid
