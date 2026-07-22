from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.cli.main import main
from enginery.domain.errors import ExternalConflictError
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.workspace import GitWorktreeBackend
from enginery.ledger.service import LedgerService


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _dispatch_materialized_workspace(
    ledger: LedgerService, *, tmp_path: Path, run_id: str, repository_id: str, now: datetime
) -> None:
    """Drive a real node through CoordinatorRuntime.tick so its workspace reservation
    reaches "materialized" -- the live-leased state a release must refuse."""
    repository, base_revision = _repository(tmp_path)
    runtime = CoordinatorRuntime(ledger, owner="operator")
    request = FixtureDispatch(
        run_id=run_id,
        node_id="node-1",
        attempt_id="attempt-1",
        repository_id=repository_id,
        repository_path=repository,
        workspace_path=tmp_path / f"workspace-{repository_id}",
        base_revision=base_revision,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        expected_attempt_version=0,
        operation_id=f"operation-{run_id}",
    )
    tick = runtime.tick(
        now=now,
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        requests=(request,),
    )
    assert len(tick.dispatched) == 1


def test_workspace_inspect_reports_no_reservations_on_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"

    exit_code = main(
        ["workspace", "inspect", "--database", str(database), "--owner", "operator", "--json"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_workspace_inspect_lists_a_materialized_reservation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger,
            tmp_path=tmp_path,
            run_id="run-1",
            repository_id="repository-1",
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    finally:
        ledger.close()

    exit_code = main(
        ["workspace", "inspect", "--database", str(database), "--owner", "operator", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["repository_id"] == "repository-1"
    assert payload[0]["run_id"] == "run-1"
    assert payload[0]["status"] == "materialized"


def test_workspace_release_dry_run_reports_would_release_false_for_a_live_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger,
            tmp_path=tmp_path,
            run_id="run-1",
            repository_id="repository-1",
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    finally:
        ledger.close()

    exit_code = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "repository-1",
            "--run-id",
            "run-1",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_release"] is False
    assert "retained" in payload["reason"]
    assert payload["reservation"]["status"] == "materialized"

    # The dry run must not have mutated anything: the reservation is still there,
    # still materialized, untouched by the destructive path.
    ledger = LedgerService.open(database)
    try:
        reservation = GitWorktreeBackend(
            ledger, CoordinatorRuntime(ledger, owner="operator").coordinator
        ).read_reservation("repository-1")
    finally:
        ledger.close()
    assert reservation is not None
    assert reservation.status == "materialized"


def test_workspace_release_refuses_a_live_leased_materialized_reservation(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger,
            tmp_path=tmp_path,
            run_id="run-1",
            repository_id="repository-1",
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    finally:
        ledger.close()

    exit_code = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "repository-1",
            "--run-id",
            "run-1",
        ]
    )

    assert exit_code != 0

    ledger = LedgerService.open(database)
    try:
        reservation = GitWorktreeBackend(
            ledger, CoordinatorRuntime(ledger, owner="operator").coordinator
        ).read_reservation("repository-1")
    finally:
        ledger.close()
    assert reservation is not None
    assert reservation.status == "materialized"
    assert reservation.workspace_path.is_dir()


def test_workspace_release_refuses_via_the_library_call_too(tmp_path: Path) -> None:
    """Same refusal, exercised directly against CoordinatorRuntime.release_workspace --
    proves the CLI reuses the coordinator's own fenced-proof check, not a weaker one."""
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger,
            tmp_path=tmp_path,
            run_id="run-1",
            repository_id="repository-1",
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
        runtime = CoordinatorRuntime(ledger, owner="operator")
        epoch = runtime.claim_epoch(
            now=datetime(2026, 7, 22, 12, 0, 1, tzinfo=UTC), heartbeat_window=timedelta(seconds=60)
        )

        with pytest.raises(ExternalConflictError, match="retained workspace"):
            runtime.release_workspace(
                run_id="run-1",
                repository_id="repository-1",
                epoch=epoch.epoch,
                now=datetime(2026, 7, 22, 12, 0, 2, tzinfo=UTC),
            )
    finally:
        ledger.close()


def test_workspace_release_succeeds_for_a_retained_reservation_with_no_live_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger, tmp_path=tmp_path, run_id="run-1", repository_id="repository-1", now=now
        )
    finally:
        ledger.close()

    # Reconstruct the dispatched fixture description and retain it, matching the
    # real human-wait path that produces a "retained" (no live lease) reservation.
    ledger = LedgerService.open(database)
    try:
        runtime = CoordinatorRuntime(ledger, owner="operator")
        dispatched = runtime.recover_dispatched(run_id="run-1", node_id="node-1")
        runtime.enter_human_wait(
            dispatched=dispatched, reason="test setup", now=now + timedelta(seconds=1)
        )
    finally:
        ledger.close()

    dry_run_exit = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "repository-1",
            "--run-id",
            "run-1",
            "--dry-run",
            "--json",
        ]
    )
    assert dry_run_exit == 0
    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["would_release"] is True

    release_exit = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "repository-1",
            "--run-id",
            "run-1",
            "--json",
        ]
    )

    assert release_exit == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cleaned"

    ledger = LedgerService.open(database)
    try:
        reservation = GitWorktreeBackend(
            ledger, CoordinatorRuntime(ledger, owner="operator").coordinator
        ).read_reservation("repository-1")
    finally:
        ledger.close()
    assert reservation is not None
    assert reservation.status == "cleaned"
    assert not reservation.workspace_path.exists()


def test_workspace_release_rejects_a_mismatched_run_id(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger,
            tmp_path=tmp_path,
            run_id="run-1",
            repository_id="repository-1",
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    finally:
        ledger.close()

    exit_code = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "repository-1",
            "--run-id",
            "run-does-not-own-this-reservation",
        ]
    )

    assert exit_code != 0


def test_workspace_release_rejects_an_unknown_repository_id(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    exit_code = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "no-such-repository",
            "--run-id",
            "run-1",
        ]
    )

    assert exit_code != 0


def test_workspace_release_dry_run_reports_no_reservation_for_unknown_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()

    exit_code = main(
        [
            "workspace",
            "release",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--repository-id",
            "no-such-repository",
            "--run-id",
            "run-1",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_release"] is False


def test_workspace_inspect_text_output_lists_status_and_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _dispatch_materialized_workspace(
            ledger,
            tmp_path=tmp_path,
            run_id="run-1",
            repository_id="repository-1",
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )
    finally:
        ledger.close()

    exit_code = main(["workspace", "inspect", "--database", str(database), "--owner", "operator"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[materialized] repository-1 run=run-1" in out
