"""Temporary old/new command parity; removed with the superseded parsers."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.cli import factory_control
from enginery.cli.factory_collect import database_path
from enginery.cli.main import _build_parser, main
from enginery.ledger.service import LedgerService
from tests.cli.test_factory_run import bound_project as bound_project
from tests.cli.test_factory_run import invoke
from tests.cli.test_stage1 import _start_request
from tests.cli.test_stage2 import _seed_stack
from tests.support.github import StandInGitHub

_NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def _json_command(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    assert captured.out, (argv, captured.err)
    document = json.loads(captured.out)
    assert isinstance(document, dict)
    assert code == 0 or document.get("healthy") is False
    return code, document


def test_transitional_root_command_names() -> None:
    # Keep temporary compatibility names out of permanent guards.
    from tests.cli.test_command_surface import _ROOT_COMMANDS, _subparsers

    commands = _subparsers(_build_parser())
    assert commands is not None
    assert set(commands.choices) == _ROOT_COMMANDS | {"factory", "stage1", "stage2", "ledger"}


@pytest.mark.parametrize("per_run", [False, True], ids=["project-summary", "run-detail"])
def test_project_status_json_matches(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    per_run: bool,
) -> None:
    project, _ = bound_project
    code, created = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0
    run_id = str(created["run_id"])
    tail = ([run_id] if per_run else []) + ["--project", str(project), "--json"]
    # Keep SQLite sidecars present across both read-only reports.
    with LedgerService.open(database_path(project)):
        old = _json_command(capsys, ["factory", "status", *tail])
        new = _json_command(capsys, ["status", *tail])
    assert old == new
    if per_run:
        assert new[1]["run_id"] == run_id
    else:
        runs = new[1]["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        assert runs[0]["run_id"] == run_id


def test_project_pause_and_resume_lift_json_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(factory_control, "_now", lambda: _NOW)
    tail = ["--project", str(project), "--json"]
    pause = ["pause", "--reason", "maintenance", *tail]
    old_pause = _json_command(capsys, ["factory", *pause])
    _json_command(capsys, ["factory", "pause", "--lift", *tail])
    new_pause = _json_command(capsys, pause)
    assert old_pause == new_pause
    assert new_pause[1]["paused"] is True
    assert new_pause[1]["changed"] is True

    old_resume = _json_command(capsys, ["factory", "pause", "--lift", *tail])
    _json_command(capsys, ["factory", *pause])
    new_resume = _json_command(capsys, ["resume", *tail])
    assert old_resume == new_resume
    assert new_resume[1]["paused"] is False
    assert new_resume[1]["changed"] is True


def test_engine_next_json_matches_existing_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    request = _start_request(database, tmp_path)
    capsys.readouterr()
    tail = [
        "--database",
        str(database),
        "--owner",
        "stage1-cli",
        "--run-id",
        str(request.run.id),
    ]
    old = _json_command(capsys, ["stage1", "watch", *tail])
    new = _json_command(capsys, ["engine", "next", *tail])
    assert old == new
    assert new[1]["run_id"] == str(request.run.id)
    assert new[1]["next_action"] == "qualify"


def test_engine_status_json_matches_existing_stack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    _seed_stack(database)
    tail = ["--database", str(database), "--owner", "test", "--stack-id", "stack-1"]
    old = _json_command(capsys, ["stage2", "status", *tail])
    new = _json_command(capsys, ["engine", "status", *tail])
    assert old == new
    assert new[1]["found"] is True
    slices = new[1]["slices"]
    assert isinstance(slices, list) and slices[0]["milestone_id"] == "m1"


def test_operator_status_json_matches_standalone_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    LedgerService.open(database).close()
    tail = ["status", "--database", str(database)]
    completed = subprocess.run(
        [str(Path(sys.executable).with_name("enginery-operator")), *tail],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    old = (completed.returncode, json.loads(completed.stdout))
    new = _json_command(capsys, ["operator", *tail])
    assert old == new
    health = new[1]["ledger"]
    assert isinstance(health, dict) and health["healthy"] is True


def test_operator_verify_json_matches_ledger_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.sqlite"
    LedgerService.open(database).close()
    tail = ["verify", "--database", str(database), "--json"]
    old = _json_command(capsys, ["ledger", *tail])
    new = _json_command(capsys, ["operator", *tail])
    assert old == new
    assert new[1]["healthy"] is True
    assert new[1]["issues"] == []


def test_operator_verify_missing_ledger_matches_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _json_command(
        capsys, ["ledger", "verify", "--database", str(tmp_path / "old-missing.sqlite"), "--json"]
    )
    new = _json_command(
        capsys, ["operator", "verify", "--database", str(tmp_path / "new-missing.sqlite"), "--json"]
    )
    assert old == new
    assert new[0] == 12
    assert new[1]["healthy"] is False
