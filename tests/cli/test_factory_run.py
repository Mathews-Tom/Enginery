from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.factory_intake import derive_run_id
from enginery.cli import factory_run
from enginery.cli.config_locations import private_state_directory, project_identity
from enginery.cli.main import _build_parser, main
from enginery.domain.errors import InvalidInputError
from enginery.engine.operator_control import OperatorControl
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.leases import LeaseWrite
from enginery.ledger.process_manager import ProcessManagerStateWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import (
    Stage1Progression,
    Stage1ProgressionAction,
    Stage1RunService,
    stage1_request_from_state,
)
from tests.cli.test_stage1_request_builder import _git, _repository
from tests.support.github import StandInGitHub, write_stand_in_gh


@pytest.fixture
def bound_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, StandInGitHub]:
    project, _ = _repository(tmp_path)
    _git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=project)
    github = write_stand_in_gh(tmp_path / "github")
    harness = tmp_path / "omp"
    harness.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "if '--help' in sys.argv:\n print('omp v18.10.3')\n"
        "else:\n"
        " for event in ('session', 'agent_start', 'agent_end'):\n"
        "  print(json.dumps({'type': event}))\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)
    (project / ".enginery").mkdir()
    (project / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n', encoding="utf-8"
    )
    user = Path(os.environ["XDG_CONFIG_HOME"]) / "enginery" / "user.toml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(
        f'schema_version = 1\n[executables]\ngithub = "{github.executable}"\n'
        f'harness = "{harness}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(factory_run, "intake_prerequisites", lambda _: [])
    return project, github


def invoke(capsys: pytest.CaptureFixture[str], project: Path, *arguments: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = main(["factory", *arguments, "--project", str(project), "--json"])
    output = capsys.readouterr()
    assert output.out, output.err
    return code, json.loads(output.out)


def test_run_binds_compiler_request_and_status_explains_embedded_selection(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    code, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0
    raw = json.loads(Path(run["request_path"]).read_text())
    request = stage1_request_from_state(raw)
    assert run["run_id"] == derive_run_id(
        project_identity(project), "owner/repo#1", str(request.work_snapshot.bound_digest)
    )
    assert (
        request.workspace_path
        == private_state_directory(project) / "runs" / run["run_id"] / "workspace"
    )
    assert request.implementation.permitted_capabilities == tuple(run["permitted_capabilities"])
    code, status = invoke(capsys, project, "status", run["run_id"])
    assert code == 0 and status["request_digest"] == str(request.digest)
    assert status["budget"]["cost"] == "unenforced"
    assert status["selection"] == run["selection"]
    code, explanation = invoke(capsys, project, "explain", run["run_id"])
    assert code == 0 and explanation["fields"] == run["fields"]
    assert (
        next(field for field in explanation["fields"] if field["name"] == "runner_mode")["winner"]
        == "project"
    )
    _, listing = invoke(capsys, project, "status")
    assert [item["run_id"] for item in listing["runs"]] == [run["run_id"]]
    rebuilt = project.parent / "stage1-request.json"
    assert (
        main(
            [
                "stage1",
                "build-request",
                "--output",
                str(rebuilt),
                "--run-id",
                run["run_id"],
                "--repository",
                "owner/repo",
                "--external-reference",
                "owner/repo#1",
                "--repository-path",
                str(project),
                "--workspace-path",
                str(request.workspace_path),
                "--artifact-root",
                str(request.execution_configuration.artifact_root),
                "--base-branch",
                request.base_branch,
                "--base-revision",
                request.run.base_revision,
                "--capability-lockfile",
                str(project / ".enginery" / "capabilities.lock.json"),
            ]
        )
        == 0
    )
    assert json.loads(rebuilt.read_text()) == raw


def test_changed_issue_holds_without_replacing_request(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, github = bound_project
    _, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    path = Path(run["request_path"])
    before = path.read_bytes()
    github.serve(updated_at="2026-10-17T00:00:00Z")
    code, held = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 5
    assert held["holds"][0]["detail"] == f"issue changed since run {run['run_id']} was bound"
    assert "stage1 cancel" not in held["holds"][0]["resolve"]
    repairs = held["holds"][0]["resolve"].split("; ")
    assert shlex.split(repairs[0]) == [
        "enginery",
        "factory",
        "drain",
        "--stop",
        "--project",
        str(project),
    ]
    assert shlex.split(repairs[1]) == [
        "enginery",
        "factory",
        "pause",
        "--lift",
        "--project",
        str(project),
    ]
    assert shlex.split(repairs[2]) == [
        "enginery",
        "factory",
        "run",
        "owner/repo#1",
        "--supersede",
        "--project",
        str(project),
    ]
    assert path.read_bytes() == before


def test_repeat_run_reuses_durable_request_and_workspace(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    _, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    path = Path(run["request_path"])
    before = path.read_bytes()
    code, repeated = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0 and repeated["run_id"] == run["run_id"]
    assert path.read_bytes() == before
    assert len(list(path.parent.parent.glob("*/request.json"))) == 1
    assert (
        main(
            [
                "stage1",
                "cancel",
                "--database",
                run["database"],
                "--owner",
                "factory",
                "--run-id",
                run["run_id"],
                "--node-id",
                "implement",
            ]
        )
        == 0
    )


@pytest.mark.parametrize("value", ("-1", "nan", "inf", "-inf"))
def test_invalid_wait_seconds_is_rejected_before_any_effect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "factory",
                "run",
                "owner/repo#1",
                "--project",
                str(tmp_path),
                f"--wait-seconds={value}",
            ]
        )
    assert error.value.code == 2
    assert "finite non-negative number" in capsys.readouterr().err
    assert not private_state_directory(tmp_path).exists()


def test_adapter_refuses_padded_issue_numbers(bound_project: tuple[Path, StandInGitHub]) -> None:
    _, github = bound_project
    adapter = GitHubWorkLedger(
        GitHubAdapterConfig(
            repository="owner/repo",
            credential_reference="github",
            executable=str(github.executable),
        )
    )
    with pytest.raises(InvalidInputError, match="positive issue number"):
        adapter.fetch("owner/repo#01")
    assert not github.calls()


def test_normalized_issue_spellings_share_one_bound_run(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    adapter = factory_run.github_ledger(
        factory_run.load_project_configuration(project), "owner/repo"
    )
    snapshot = adapter.fetch("owner/repo#1")
    monkeypatch.setattr(
        factory_run, "github_ledger", lambda *_: SimpleNamespace(fetch=lambda _: snapshot)
    )
    _, run = invoke(capsys, project, "run", "owner/repo#1", "--once")
    _, repeated = invoke(capsys, project, "run", "OWNER/REPO#1", "--once")
    assert repeated["run_id"] == run["run_id"]
    assert repeated["issue"] == "owner/repo#1"
    index = json.loads(factory_run.index_path(project).read_text())
    assert list(index["repositories"]["owner/repo"]["issues"]) == ["owner/repo#1"]
    assert len(list(Path(run["request_path"]).parent.parent.glob("*/request.json"))) == 1


@pytest.mark.parametrize(
    ("action", "wait_seconds", "expected_calls", "expected_sleeps"),
    [
        (Stage1ProgressionAction.WAIT, 9.0, 1, [0.5, 1.0, 2.0, 5.0, 0.5]),
        (Stage1ProgressionAction.DISPATCH_QUEUED, 9.0, 1, [0.5, 1.0, 2.0, 5.0, 0.5]),
        (Stage1ProgressionAction.RECONCILE_WORKER, 0.0, 1, []),
        (Stage1ProgressionAction.WAIT_FOR_CI, 31.0, 3, [15.0, 15.0, 1.0]),
        (Stage1ProgressionAction.QUALIFY, 3.0, 1, [0.5, 1.0, 1.5]),
        (Stage1ProgressionAction.IMPLEMENT, 3.0, 1, [0.5, 1.0, 1.5]),
        (Stage1ProgressionAction.COLLECT_IMPLEMENTATION, 3.0, 1, [0.5, 1.0, 1.5]),
        (Stage1ProgressionAction.VALIDATE, 3.0, 1, [0.5, 1.0, 1.5]),
        (Stage1ProgressionAction.OPEN_PR, 3.0, 1, [0.5, 1.0, 1.5]),
        (
            Stage1ProgressionAction.VERIFY,
            31.0,
            2,
            [0.5, 1.0, 2.0, 5.0, 5.0, 5.0, 5.0, 5.0, 2.5],
        ),
        (Stage1ProgressionAction.REGISTER_OUTCOME_OBSERVATION, 3.0, 1, [0.5, 1.0, 1.5]),
        (Stage1ProgressionAction.HOLD, 30.0, 0, []),
        (Stage1ProgressionAction.BLOCKED, 30.0, 0, []),
        (Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, 30.0, 0, []),
    ],
)
def test_wait_loop_backs_off_and_stops_at_deadline_without_spinning(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    action: Stage1ProgressionAction,
    wait_seconds: float,
    expected_calls: int,
    expected_sleeps: list[float],
) -> None:
    project, _ = bound_project
    _, report = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(report["database"]))
    try:
        runtime = factory_run.runtime_for_run(ledger, owner="factory", run_id=report["run_id"])
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(
            factory_run.RunId(report["run_id"])
        )
    finally:
        ledger.close()
    progression = Stage1Progression(action, run)
    calls: list[float] = []
    sleeps: list[float] = []
    clock = 0.0

    def advance(*_: object, **__: object) -> Stage1Progression:
        calls.append(clock)
        return progression

    def sleep(seconds: float) -> None:
        nonlocal clock
        assert seconds > 0
        sleeps.append(seconds)
        clock += seconds

    monkeypatch.setattr(
        factory_run,
        "nodes",
        lambda *_args, **_kwargs: [
            {"node_id": "implement", "status": "queued", "attempt_id": "implement-0"}
        ],
    )
    watch = factory_run.watch_document

    def waiting_watch(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            **watch(*args, **kwargs),
            "nodes": [{"node_id": "implement", "status": "queued", "attempt_id": "implement-0"}],
        }

    monkeypatch.setattr(factory_run, "watch_document", waiting_watch)
    monkeypatch.setattr(
        factory_run,
        "advancing_service",
        lambda **_: SimpleNamespace(next_action=lambda _: progression, advance=advance),
    )
    monkeypatch.setattr(factory_run.time, "monotonic", lambda: clock)
    monkeypatch.setattr(factory_run.time, "sleep", sleep)
    result = factory_run.advance_run(project, report["run_id"], wait_seconds=wait_seconds)
    assert result["next_action"] == action.value
    assert len(calls) == expected_calls
    assert all(later - earlier >= 15.0 for earlier, later in pairwise(calls))
    assert sleeps == expected_sleeps
    assert clock == sum(expected_sleeps)


@pytest.mark.parametrize("idle", (False, True))
def test_advance_stops_when_issue_binding_is_superseded(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    idle: bool,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    run_id = original["run_id"]
    ledger = LedgerService.open(Path(original["database"]))
    try:
        runtime = factory_run.runtime_for_run(ledger, owner="factory", run_id=run_id)
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(factory_run.RunId(run_id))
    finally:
        ledger.close()
    initial = Stage1Progression(Stage1ProgressionAction.QUALIFY, run)
    after = Stage1Progression(
        Stage1ProgressionAction.WAIT_FOR_CI if idle else Stage1ProgressionAction.IMPLEMENT,
        run,
    )
    calls = 0
    replacement = "replacement-run"

    def advance(*_args: object, **_kwargs: object) -> Stage1Progression:
        nonlocal calls
        calls += 1
        assert calls == 1, "superseded run must not advance again"
        index = factory_run.read_index(project)
        intake = index.repositories[str(run.request.repository_id)]
        entry = intake.issues["owner/repo#1"]
        intake.issues["owner/repo#1"] = type(entry)(
            external_reference=entry.external_reference,
            run_id=replacement,
            bound_digest=entry.bound_digest,
            source_revision=entry.source_revision,
            recorded_at=entry.recorded_at,
        )
        factory_run.write_document(factory_run.index_path(project), index.to_state())
        return after

    monkeypatch.setattr(
        factory_run,
        "advancing_service",
        lambda **_: SimpleNamespace(next_action=lambda _: initial, advance=advance),
    )
    monkeypatch.setattr(factory_run.time, "sleep", lambda _: None)
    report = factory_run.advance_run(project, run_id, wait_seconds=30)
    assert calls == 1
    assert report["superseded_by"] == replacement
    assert report["holds"][-1]["prerequisite"] == "superseded binding"
    assert report["holds"][-1]["detail"] == f"superseded by run {replacement}"


def test_supersede_refuses_live_run_and_accepts_cancelled_run(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, github = bound_project
    # The worker must still be running when the repair cancels it: an exited worker launched
    # in-process stays an unreaped child of this test, and signalling a zombie-only process
    # group fails on macOS, which no real out-of-process operator would see.
    harness = project.parent / "omp"
    harness.write_text(
        f"#!{sys.executable}\nimport json, sys, time\n"
        "if '--help' in sys.argv:\n print('omp v18.10.3')\n"
        "else:\n"
        " print(json.dumps({'type': 'session'}), flush=True)\n"
        " time.sleep(600)\n",
        encoding="utf-8",
    )
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    invoke(capsys, project, "run", "owner/repo#1", "--once")
    github.serve(updated_at="2026-10-17T00:00:00Z")
    code, held = invoke(capsys, project, "run", "owner/repo#1", "--supersede", "--once")
    assert code == 5
    assert f"--run-id {original['run_id']}" in held["holds"][0]["resolve"]
    assert "--node-id implement" in held["holds"][0]["resolve"]
    assert "run.status=" in held["holds"][0]["detail"]
    assert f"runtime_node[{original['run_id']}:implement].status=" in held["holds"][0]["detail"]
    assert f"node_leases[{original['run_id']}:implement].expires_at=" in held["holds"][0]["detail"]
    assert f"runtime_node[{original['run_id']}:open_pr].status=" in held["holds"][0]["detail"]
    assert len(list(Path(original["request_path"]).parent.parent.glob("*/request.json"))) == 1
    try:
        for command in held["holds"][0]["resolve"].split("; "):
            capsys.readouterr()
            assert main(shlex.split(command)[1:]) == 0
            capsys.readouterr()
        _, listing = invoke(capsys, project, "status")
        replacement = next(run for run in listing["runs"] if run["run_id"] != original["run_id"])
        assert replacement["run_id"] != original["run_id"]
        _, old = invoke(capsys, project, "status", original["run_id"])
        assert old["status"] == "created"
        implementation = next(node for node in old["nodes"] if node["node_id"] == "implement")
        assert implementation["status"] == "cancelled"
    finally:
        _, listing = invoke(capsys, project, "status")
        for run in listing["runs"]:
            if any(
                node["node_id"] == "implement" and node["status"] == "running"
                for node in run["nodes"]
            ):
                main(
                    [
                        "stage1",
                        "cancel",
                        "--database",
                        str(listing["database"]),
                        "--owner",
                        "factory",
                        "--run-id",
                        str(run["run_id"]),
                        "--node-id",
                        "implement",
                    ]
                )
        capsys.readouterr()


def test_unstarted_request_is_rebuilt_when_configuration_changes(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    start = factory_run.start_bound_run

    def interrupted(*_: object, **__: object) -> None:
        raise RuntimeError("interrupted before binding")

    monkeypatch.setattr(factory_run, "start_bound_run", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        invoke(capsys, project, "run", "owner/repo#1", "--once")
    path = next((private_state_directory(project) / "runs").glob("*/request.json"))
    old = stage1_request_from_state(json.loads(path.read_text()))
    policy = project / ".enginery" / "project.toml"
    policy.write_text(policy.read_text() + "[implementation]\ntime_budget_seconds = 123\n")
    monkeypatch.setattr(factory_run, "start_bound_run", start)
    code, report = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0
    assert report["request_rebuilt"] is True
    assert report["request_digest"] != str(old.digest)
    assert report["budget"]["time_budget_seconds"] == 123


def test_repeat_run_skips_bound_start(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")

    def unexpected_start(*_: object, **__: object) -> None:
        pytest.fail("an existing projection must not start again")

    monkeypatch.setattr(factory_run, "start_bound_run", unexpected_start)
    code, repeated = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0
    assert repeated["run_id"] == original["run_id"]


def test_paused_run_preserves_execution_holds(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(original["database"]))
    try:
        OperatorControl(ledger).pause(now=datetime.now(UTC), reason="inspect")
    finally:
        ledger.close()
    watch = factory_run.watch_document

    def held_watch(
        ledger: LedgerService,
        runtime: CoordinatorRuntime,
        progression: Stage1Progression,
        *,
        action_taken: str | None = None,
    ) -> dict[str, object]:
        return {
            **watch(ledger, runtime, progression, action_taken=action_taken),
            "hold": {"reason": "worker_identity_unprovable", "message": "worker is remote"},
        }

    monkeypatch.setattr(factory_run, "watch_document", held_watch)
    report = factory_run.advance_run(project, original["run_id"])
    assert len(report["holds"]) == 2
    assert report["holds"][0]["message"] == "worker is remote"
    assert report["holds"][0]["resolve"] == "Resolve the worker on its launching host"
    assert shlex.split(report["holds"][1]["resolve"]) == [
        "enginery",
        "factory",
        "pause",
        "--lift",
        "--project",
        str(project),
    ]


@pytest.mark.parametrize(
    ("hold", "expected"),
    [
        ({"reason": "catalog_drift", "message": "catalog changed"}, "restore"),
        ({"reason": "runner_fingerprint_drift", "message": "runner changed"}, "restore"),
        ({"reason": "custom", "message": "Resolve the specific service conflict"}, "message"),
        ({"reason": "custom", "message": "held", "repair": "service repair --exact"}, "repair"),
        (
            {
                "reason": "custom",
                "message": "held",
                "details": {"repair": "service repair --nested"},
            },
            "nested",
        ),
    ],
)
def test_status_preserves_service_hold_diagnostics_and_matching_repairs(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    hold: dict[str, object],
    expected: str,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    watch = factory_run.watch_document

    def held_watch(
        ledger: LedgerService,
        runtime: CoordinatorRuntime,
        progression: Stage1Progression,
        *,
        action_taken: str | None = None,
    ) -> dict[str, object]:
        return {
            **watch(ledger, runtime, progression, action_taken=action_taken),
            "next_action": "blocked",
            "hold": hold,
        }

    monkeypatch.setattr(factory_run, "watch_document", held_watch)
    code, report = invoke(capsys, project, "status", original["run_id"])
    assert code == 5
    assert report["holds"][0]["message"] == hold["message"]
    doctor = f"enginery factory doctor --project {shlex.quote(str(project))}"
    assert (
        report["holds"][0]["resolve"]
        == {
            "restore": (
                f"{doctor}; restore the run's bound configuration; "
                f"enginery factory run {shlex.quote(original['issue'])} "
                f"--project {shlex.quote(str(project))}"
            ),
            "message": f"{doctor}; {hold['message']}",
            "repair": "service repair --exact",
            "nested": "service repair --nested",
        }[expected]
    )


@pytest.mark.parametrize("generation", (None, 0, 3))
def test_human_review_reports_the_current_repair_decision_command(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    generation: int | None,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    watch = factory_run.watch_document

    def review_watch(
        ledger: LedgerService,
        runtime: CoordinatorRuntime,
        progression: Stage1Progression,
        *,
        action_taken: str | None = None,
    ) -> dict[str, object]:
        report = watch(ledger, runtime, progression, action_taken=action_taken)
        report["next_action"] = "await_human_review"
        if generation is not None:
            report["nodes"] = [
                {
                    "node_id": "implement",
                    "status": "passed",
                    "attempt_id": f"implement-{generation}",
                }
            ]
        report["nodes"] = [
            *report["nodes"],
            {"node_id": "review", "status": "awaiting_human", "attempt_id": "review-0"},
        ]
        return report

    monkeypatch.setattr(factory_run, "watch_document", review_watch)
    code, report = invoke(capsys, project, "status", original["run_id"])
    assert code == 0 and report["holds"] == []
    assert report["next_decision"]["action"] == "stage1 review"
    assert shlex.split(report["next_decision"]["command"]) == [
        "enginery",
        "stage1",
        "review",
        "--database",
        original["database"],
        "--owner",
        "factory",
        "--run-id",
        original["run_id"],
        "--report",
        "REPORT.json",
        "--repair-attempt",
        str(generation or 0),
    ]


def test_blocked_progression_without_execution_hold_reports_its_reason(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    watch = factory_run.watch_document
    reason = "Name validation commands that run with the tools in the image"

    def blocked_watch(
        ledger: LedgerService,
        runtime: CoordinatorRuntime,
        progression: Stage1Progression,
        *,
        action_taken: str | None = None,
    ) -> dict[str, object]:
        return {
            **watch(ledger, runtime, progression, action_taken=action_taken),
            "next_action": "blocked",
            "reason": reason,
        }

    monkeypatch.setattr(factory_run, "watch_document", blocked_watch)
    code, report = invoke(capsys, project, "status", original["run_id"])
    assert code == 5
    assert report["holds"][0]["message"] == reason
    assert report["holds"][0]["resolve"] == (
        f"enginery factory doctor --project {shlex.quote(str(project))}; {reason}"
    )


def test_status_never_opens_the_migrating_ledger(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")

    def unexpected_open(*_: object, **__: object) -> None:
        pytest.fail("inspection must use a read-only ledger")

    monkeypatch.setattr(LedgerService, "open", unexpected_open)
    code, report = invoke(capsys, project, "status", original["run_id"])
    assert code == 0 and report["run_id"] == original["run_id"]


def test_base_branch_cannot_be_interpreted_as_a_git_option(
    bound_project: tuple[Path, StandInGitHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    calls: list[tuple[str, ...]] = []
    real_run = factory_run.subprocess.run

    def runner(argv: tuple[str, ...], **kwargs: object) -> object:
        if "rev-parse" not in argv:
            return real_run(argv, **kwargs)
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="unknown revision")

    monkeypatch.setattr(factory_run.subprocess, "run", runner)
    with pytest.raises(InvalidInputError, match="git could not resolve"):
        factory_run.request_inputs(
            project, "owner/repo#1", "fr-test", SimpleNamespace(base_branch="--help")
        )
    assert calls == [
        ("git", "-C", str(project), "rev-parse", "--verify", "--end-of-options", "--help^{commit}")
    ]


@pytest.mark.parametrize(
    ("boundary", "allowed"),
    [
        ("active_lease", False),
        ("live_worker", False),
        ("pending_publication", False),
        ("terminal_run", True),
        ("terminal_implementation", True),
    ],
)
def test_supersede_checks_durable_terminal_and_effect_boundaries(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    boundary: str,
    allowed: bool,
) -> None:
    project, github = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    run_id = original["run_id"]
    attempt_id = f"{run_id}-implement-0"
    ledger = LedgerService.open(Path(original["database"]))
    try:
        run = ledger.read_projection(aggregate_type="run", aggregate_id=run_id)
        assert run is not None
        events = [
            EventWrite(
                aggregate_type="runtime_node",
                aggregate_id=f"{run_id}:implement",
                expected_version=0,
                event_type="runtime_node.fixture_terminal",
                schema_version=1,
                payload={"status": "cancelled", "attempt_id": attempt_id},
            )
        ]
        if boundary == "terminal_run":
            events.append(
                EventWrite(
                    aggregate_type="run",
                    aggregate_id=run_id,
                    expected_version=run.aggregate_version,
                    event_type="run.fixture_terminal",
                    schema_version=1,
                    payload={**run.state, "status": "succeeded"},
                )
            )
        if boundary == "pending_publication":
            events.append(
                EventWrite(
                    aggregate_type="runtime_node",
                    aggregate_id=f"{run_id}:open_pr",
                    expected_version=0,
                    event_type="runtime_node.fixture_pending",
                    schema_version=1,
                    payload={"status": "queued", "attempt_id": f"{run_id}-open_pr-0"},
                )
            )
        ledger.append(
            AppendCommand(
                correlation_id=f"supersede-fixture:{boundary}",
                events=tuple(events),
                lease_updates=(
                    (
                        LeaseWrite(
                            run_id=run_id,
                            node_id="implement",
                            epoch=1,
                            fencing_token=1,
                            owner="factory",
                            attempt_id=attempt_id,
                            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                        ),
                    )
                    if boundary == "active_lease"
                    else ()
                ),
                process_manager_updates=(
                    (
                        ProcessManagerStateWrite(
                            "worker-supervisor",
                            f"{run_id}:implement",
                            0,
                            {
                                "status": "launch_intended",
                                "run_id": run_id,
                                "node_id": "implement",
                                "attempt_id": attempt_id,
                                "operation_id": f"implement:{run_id}",
                            },
                        ),
                    )
                    if boundary == "live_worker"
                    else ()
                ),
            )
        )
    finally:
        ledger.close()
    github.serve(updated_at="2026-10-17T00:00:00Z")
    code, report = invoke(capsys, project, "run", "owner/repo#1", "--supersede", "--once")
    assert code == (0 if allowed else 5)
    if allowed:
        assert report["run_id"] != run_id
    else:
        assert "supersede not safe" in report["holds"][0]["detail"]
        assert "--supersede" in report["holds"][0]["resolve"]


@pytest.mark.parametrize("risk", ("low", "medium"))
def test_supersede_accepts_never_registered_implementation(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    risk: str,
) -> None:
    project, github = bound_project
    github.serve(labels=("enginery/work-kind/issue", f"enginery/risk/{risk}"))
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert all(node["node_id"] != "implement" for node in original["nodes"])
    github.serve(updated_at="2026-10-17T00:00:00Z")
    code, held = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 5
    assert "--node-id implement" not in held["holds"][0]["resolve"]
    assert "--node-id qualify" not in held["holds"][0]["resolve"]
    code, replacement = invoke(capsys, project, "run", "owner/repo#1", "--supersede", "--once")
    assert code == 0
    assert replacement["run_id"] != original["run_id"]


@pytest.mark.parametrize("failed", (False, True))
def test_supersede_unchanged_snapshot_is_invalid(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    failed: bool,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(original["database"]))
    try:
        ledger.append(
            AppendCommand(
                correlation_id="unchanged-implementation",
                events=(
                    EventWrite(
                        aggregate_type="runtime_node",
                        aggregate_id=f"{original['run_id']}:implement",
                        expected_version=0,
                        event_type="runtime_node.fixture_status",
                        schema_version=1,
                        payload={
                            "status": "failed" if failed else "running",
                            "attempt_id": "implement-0",
                        },
                    ),
                ),
                lease_updates=(
                    ()
                    if failed
                    else (
                        LeaseWrite(
                            run_id=original["run_id"],
                            node_id="implement",
                            epoch=1,
                            fencing_token=1,
                            owner="factory",
                            attempt_id="implement-0",
                            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                        ),
                    )
                ),
            )
        )
    finally:
        ledger.close()
    code = main(
        [
            "factory",
            "run",
            "owner/repo#1",
            "--supersede",
            "--once",
            "--project",
            str(project),
            "--json",
        ]
    )
    assert code == 2
    assert (
        f"issue snapshot unchanged since run {original['run_id']} was bound; "
        f"use enginery factory run {shlex.quote('owner/repo#1')} "
        f"--project {shlex.quote(str(project))}"
    ) in capsys.readouterr().err


def test_medium_risk_wait_names_approve_and_reject(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, github = bound_project
    github.serve(labels=("enginery/work-kind/issue", "enginery/risk/medium"))
    code, report = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 0
    decision = report["next_decision"]
    for key, action in (("command", "approve"), ("reject_command", "reject")):
        argv = shlex.split(decision[key])
        parsed = _build_parser().parse_args(argv[1:])
        assert parsed.reason == "REASON"
        assert argv[:3] == ["enginery", "stage1", action]
        assert argv[argv.index("--node-id") + 1] == "qualify"
        assert argv[argv.index("--run-id") + 1] == report["run_id"]
        assert argv[argv.index("--database") + 1] == report["database"]


def test_failed_implementation_without_wait_reports_service_failure(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(original["database"]))
    try:
        ledger.append(
            AppendCommand(
                correlation_id="failure-decision",
                events=(
                    EventWrite(
                        aggregate_type="runtime_node",
                        aggregate_id=f"{original['run_id']}:implement",
                        expected_version=0,
                        event_type="runtime_node.fixture_failed",
                        schema_version=1,
                        payload={
                            "status": "failed",
                            "attempt_id": "implement-0",
                            "message": "Implementation exceeded its time limit",
                            "repair": "service repair --exact",
                        },
                    ),
                ),
            )
        )
    finally:
        ledger.close()
    code, report = invoke(capsys, project, "status", original["run_id"])
    assert code == 5
    assert "next_decision" not in report
    assert report["holds"][0]["message"] == "Implementation exceeded its time limit"
    assert report["holds"][0]["resolve"] == "service repair --exact"


@pytest.mark.parametrize(
    "action", (Stage1ProgressionAction.WAIT, Stage1ProgressionAction.DISPATCH_QUEUED)
)
@pytest.mark.parametrize("wait_seconds", (0.05, 9.0, 31.0))
def test_idle_real_ledger_wait_does_not_append_per_poll(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    wait_seconds: float,
    action: Stage1ProgressionAction,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    real_service = factory_run.advancing_service
    advances: list[float] = []
    clock = 0.0

    def service(**kwargs: object) -> SimpleNamespace:
        authoritative = real_service(**kwargs)
        run = authoritative.next_action(factory_run.RunId(original["run_id"])).run
        waiting = Stage1Progression(action, run)
        ledger = kwargs["ledger"]

        def advance(*_args: object, **_kwargs: object) -> Stage1Progression:
            assert isinstance(ledger, LedgerService)
            advances.append(factory_run.time.monotonic())
            ledger.append(
                AppendCommand(
                    correlation_id=f"idle-attempt-{len(advances)}",
                    events=(
                        EventWrite(
                            aggregate_type="idle_fixture",
                            aggregate_id=f"idle-{len(advances)}",
                            expected_version=0,
                            event_type="idle_fixture.coordinator_attempt",
                            schema_version=1,
                            payload={},
                        ),
                    ),
                )
            )
            return waiting

        return SimpleNamespace(next_action=lambda _: waiting, advance=advance)

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(factory_run, "advancing_service", service)
    if wait_seconds > 1:
        monkeypatch.setattr(factory_run.time, "monotonic", lambda: clock)
        monkeypatch.setattr(factory_run.time, "sleep", sleep)
    ledger = LedgerService.open(Path(original["database"]))
    try:
        ledger.append(
            AppendCommand(
                correlation_id="queued-implementation",
                events=(
                    EventWrite(
                        aggregate_type="runtime_node",
                        aggregate_id=f"{original['run_id']}:implement",
                        expected_version=0,
                        event_type="runtime_node.fixture_queued",
                        schema_version=1,
                        payload={"status": "queued", "attempt_id": "implement-0"},
                    ),
                ),
            )
        )
        before = ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        factory_run.advance_run(project, original["run_id"], wait_seconds=wait_seconds)
        after = ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        ledger.close()
    assert after - before == len(advances)
    assert len(advances) <= 1 + int(wait_seconds // 15)
    assert all(right - left >= 15 for left, right in pairwise(advances))


def test_completed_run_returns_without_advancing_or_sleeping(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(original["database"]))
    try:
        runtime = factory_run.runtime_for_run(ledger, owner="factory", run_id=original["run_id"])
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(
            factory_run.RunId(original["run_id"])
        )
    finally:
        ledger.close()
    progression = Stage1Progression(Stage1ProgressionAction.WAIT, run)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("completed run must not advance or wait")

    monkeypatch.setattr(
        factory_run,
        "advancing_service",
        lambda **_: SimpleNamespace(next_action=lambda _: progression, advance=unexpected),
    )
    monkeypatch.setattr(factory_run.time, "sleep", unexpected)
    report = factory_run.advance_run(project, original["run_id"], wait_seconds=30)
    assert report["next_action"] == "wait"


def test_failed_implementation_human_wait_names_its_decision(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(original["database"]))
    try:
        ledger.append(
            AppendCommand(
                correlation_id="implementation-human-wait",
                events=(
                    EventWrite(
                        aggregate_type="runtime_node",
                        aggregate_id=f"{original['run_id']}:implement",
                        expected_version=0,
                        event_type="runtime_node.fixture_awaiting",
                        schema_version=1,
                        payload={
                            "status": "awaiting_human",
                            "attempt_id": "implement-0",
                            "operation_id": f"implement:{original['run_id']}",
                        },
                    ),
                ),
            )
        )
    finally:
        ledger.close()
    code, report = invoke(capsys, project, "status", original["run_id"])
    assert code == 0
    decision = report["next_decision"]
    assert "--node-id implement" in decision["command"]
    assert "--node-id implement" in decision["reject_command"]
    assert decision["action"] == "stage1 approve|reject"
    for key in ("command", "reject_command"):
        parsed = _build_parser().parse_args(shlex.split(decision[key])[1:])
        assert parsed.reason == "REASON"


def test_repeated_action_with_node_progress_advances_without_sleep(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = bound_project
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    ledger = LedgerService.open(Path(original["database"]))
    try:
        runtime = factory_run.runtime_for_run(ledger, owner="factory", run_id=original["run_id"])
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(
            factory_run.RunId(original["run_id"])
        )
    finally:
        ledger.close()
    generation = 0

    def progression() -> Stage1Progression:
        action = (
            Stage1ProgressionAction.IMPLEMENT if generation < 3 else Stage1ProgressionAction.WAIT
        )
        return Stage1Progression(action, run)

    def node_states(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "node_id": "implement",
                "attempt_id": f"implement-{generation}",
                "status": "running" if generation < 3 else "passed",
            }
        ]

    def advance(*_args: object, **_kwargs: object) -> Stage1Progression:
        nonlocal generation
        generation += 1
        return progression()

    watch = factory_run.watch_document

    def progress_watch(
        ledger: LedgerService,
        runtime: CoordinatorRuntime,
        progression: Stage1Progression,
        *,
        action_taken: str | None = None,
    ) -> dict[str, object]:
        return {
            **watch(ledger, runtime, progression, action_taken=action_taken),
            "nodes": node_states(),
        }

    def unexpected(_seconds: float) -> None:
        pytest.fail("durable node progress must not back off")

    monkeypatch.setattr(
        factory_run,
        "advancing_service",
        lambda **_: SimpleNamespace(next_action=lambda _: progression(), advance=advance),
    )
    monkeypatch.setattr(factory_run, "nodes", node_states)
    monkeypatch.setattr(factory_run, "watch_document", progress_watch)
    monkeypatch.setattr(factory_run.time, "sleep", unexpected)
    factory_run.advance_run(project, original["run_id"], wait_seconds=30)
    assert generation == 3


@pytest.mark.parametrize("risk", ("low", "medium"))
def test_changed_snapshot_repairs_execute_and_superseded_run_cannot_advance(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    risk: str,
) -> None:
    project, github = bound_project
    github.serve(labels=("enginery/work-kind/issue", f"enginery/risk/{risk}"))
    _, original = invoke(capsys, project, "run", "owner/repo#1", "--once")
    github.serve(
        labels=("enginery/work-kind/issue", f"enginery/risk/{risk}"),
        updated_at="2026-10-17T00:00:00Z",
    )
    code, held = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 5
    commands = held["holds"][0]["resolve"].split("; ")
    assert all("--node-id qualify" not in command for command in commands)
    for command in commands:
        capsys.readouterr()
        assert main(shlex.split(command)[1:]) == 0
        capsys.readouterr()
    _, replacement = invoke(capsys, project, "status")
    new_run = next(run for run in replacement["runs"] if run["run_id"] != original["run_id"])
    assert new_run["superseded_by"] is None
    _, old = invoke(capsys, project, "status", original["run_id"])
    assert old["superseded_by"] == new_run["run_id"]

    def unexpected_service(**_kwargs: object) -> None:
        pytest.fail("a superseded run must not construct an advancing service")

    monkeypatch.setattr(factory_run, "advancing_service", unexpected_service)
    report = factory_run.advance_run(project, original["run_id"])
    assert report["status"] == old["status"]
    assert report["holds"][-1] == {
        "prerequisite": "superseded binding",
        "detail": f"superseded by run {new_run['run_id']}",
        "resolve": (
            f"enginery factory status {new_run['run_id']} --project {shlex.quote(str(project))}"
        ),
    }


def test_intake_failure_repair_uses_configured_github(
    bound_project: tuple[Path, StandInGitHub],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, github = bound_project

    def fail_fetch(_issue: str) -> None:
        raise InvalidInputError("issue unavailable")

    monkeypatch.setattr(
        factory_run, "github_ledger", lambda *_args: SimpleNamespace(fetch=fail_fetch)
    )
    code, report = invoke(capsys, project, "run", "owner/repo#1", "--once")
    assert code == 5
    auth, collect = report["holds"][0]["resolve"].split("; ")
    assert shlex.split(auth) == [str(github.executable), "auth", "status"]
    assert shlex.split(collect) == [
        "enginery",
        "factory",
        "collect",
        "--dry-run",
        "--project",
        str(project),
    ]
