from __future__ import annotations

import getpass
import json
import os
import pty
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from enginery.application.human_decisions import ReviewAuthority
from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.cli import factory_decisions
from enginery.cli.main import main
from enginery.cli.stage1_composition import lease_window
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.runner import LEGACY_RUNNER_SELECTION, RunnerSelection
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.ledger.human_decisions import issue_challenge
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
    stage1_request_from_state,
)
from tests.support.behavioral import declare_behavioral_policy, fixture_behavioral_checks
from tests.support.execution import bound_document, selection_for


@pytest.fixture
def authenticated_decisions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Real TTY, packet, nonce and decision paths over the Stage 1 ledger."""
    master, slave = pty.openpty()
    registration = ReviewAuthority("operator", getpass.getuser(), os.getuid())
    original = _lifecycle
    with (
        os.fdopen(os.dup(slave), "r") as input_stream,
        os.fdopen(os.dup(slave), "w") as output_stream,
    ):
        monkeypatch.setattr(
            factory_decisions,
            "sys",
            SimpleNamespace(stdin=input_stream, stdout=output_stream),
        )
        monkeypatch.setattr(factory_decisions, "operator_registration", lambda: registration)

        def authenticated(database: Path, command: str, *arguments: str) -> int:
            if command not in {"approve", "reject", "resume"}:
                return original(database, command, *arguments)
            run_id = arguments[arguments.index("--run-id") + 1]
            with LedgerService.open(database) as ledger:
                runtime = CoordinatorRuntime(ledger, owner="stage1-cli")
                request = (
                    Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id)).request
                )
                packet = factory_decisions.build_packet(request.repository_path, ledger, request)
                _, context = factory_decisions._authenticate(None)
                issued = issue_challenge(
                    ledger.connection,
                    run_id=run_id,
                    subject_fields=packet.subject_fields(),
                    actions=factory_decisions._actions(ledger, request),
                    context=context,
                    registered_humans=registration.registered_humans,
                )
            reason = () if "--reason" in arguments else ("--reason", "operator investigated wait")
            return original(
                database,
                command,
                *arguments,
                *reason,
                "--subject-digest",
                issued.subject_digest,
                "--challenge",
                issued.challenge,
            )

        monkeypatch.setattr(
            __import__(__name__, fromlist=["_lifecycle"]), "_lifecycle", authenticated
        )
        try:
            yield
        finally:
            os.close(master)
            os.close(slave)


def test_stage1_start_watch_and_evidence_are_ledger_backed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(bound_document(_request(tmp_path))), encoding="utf-8")

    assert _start(database, request_path) == 0
    assert json.loads(capsys.readouterr().out) == {"run_id": "run-stage1", "status": "created"}

    assert _lifecycle(database, "watch", "--run-id", "run-stage1") == 0
    watched = json.loads(capsys.readouterr().out)
    assert watched["run_id"] == "run-stage1"
    assert watched["nodes"] == []
    assert watched["next_action"] == "qualify"

    assert _lifecycle(database, "evidence", "--run-id", "run-stage1") == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["request_digest"] == bound_document(_request(tmp_path))["request_digest"]
    assert evidence["source_revision"] == "issue-revision-1"


def test_stage1_cli_lease_window_exceeds_the_omp_time_budget(tmp_path: Path) -> None:
    request = _request(tmp_path)

    assert lease_window(request) == timedelta(seconds=120)


def test_stage1_restart_is_idempotent_and_rejects_changed_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _request(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(bound_document(request)), encoding="utf-8")

    assert _start(database, request_path) == 0
    capsys.readouterr()
    assert _start(database, request_path) == 0
    assert json.loads(capsys.readouterr().out) == {"run_id": "run-stage1", "status": "created"}

    changed_request = replace(
        request,
        validation_commands=(("uv", "run", "pytest", "tests/cli/test_stage1.py", "-q"),),
    )
    request_path.write_text(
        json.dumps(bound_document(changed_request)),
        encoding="utf-8",
    )
    assert _start(database, request_path) != 0
    assert "different immutable request" in capsys.readouterr().err


@pytest.mark.parametrize("cost_budget", ("not-a-decimal", "NaN", "Infinity"))
def test_stage1_rejects_invalid_persisted_implementation_budget(
    tmp_path: Path, cost_budget: str
) -> None:
    state = _request(tmp_path).initial_state()
    implementation = state["implementation"]
    assert isinstance(implementation, dict)
    implementation["cost_budget"] = cost_budget

    with pytest.raises(InvalidInputError, match="cost"):
        stage1_request_from_state(state)


@pytest.mark.usefixtures("authenticated_decisions")
def test_stage1_refuses_requeue_without_changing_wait_or_consuming_challenge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _start_request(database, tmp_path)
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="plan_approval")

    assert (
        _lifecycle(
            database,
            "resume",
            "--run-id",
            "run-stage1",
            "--node-id",
            "plan_approval",
            "--attempt-id",
            "plan-approval-1",
            "--operation-id",
            "plan-approval:run-stage1:1",
        )
        == 2
    )
    assert "human_wait_requeue_unsupported" in capsys.readouterr().err
    assert _node_state(database, "plan_approval")["status"] == "awaiting_human"

    assert (
        _lifecycle(
            database,
            "reject",
            "--run-id",
            "run-stage1",
            "--node-id",
            "plan_approval",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    _register_human_node(database, request, node_id="no_change_confirmation")
    assert (
        _lifecycle(
            database,
            "approve",
            "--run-id",
            "run-stage1",
            "--node-id",
            "no_change_confirmation",
            "--reason",
            "operator confirmed non-applicability",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


@pytest.mark.usefixtures("authenticated_decisions")
@pytest.mark.parametrize("command", ["approve", "reject"])
def test_stage1_human_decisions_on_a_pinned_run_record_its_selection_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    pinned = selection_for("omp", model="openai/gpt-5.2", effort="high")
    database = tmp_path / "ledger.db"
    request = _start_request(database, tmp_path, selection=pinned)
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="no_change_confirmation")

    assert (
        _lifecycle(
            database,
            command,
            "--run-id",
            "run-stage1",
            "--node-id",
            "no_change_confirmation",
            "--reason",
            "operator decision",
        )
        == 0
    )

    state = _node_state(database, "no_change_confirmation")
    assert state["execution_selection_digest"] == str(pinned.digest)
    assert state["status"] == ("passed" if command == "approve" else "failed")


@pytest.mark.usefixtures("authenticated_decisions")
def test_stage1_human_decisions_on_a_pre_selection_run_record_no_selection_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    legacy = replace(
        _request(tmp_path), execution_selection=ExecutionSelection.legacy_host_default("omp")
    )
    ledger = LedgerService.open(database)
    try:
        CoordinatorRuntime(ledger, owner="stage1-cli").register_run(
            run_id="run-stage1",
            initial_state=legacy.initial_state(),
            now=datetime.now(tz=UTC),
            heartbeat_window=timedelta(seconds=60),
        )
    finally:
        ledger.close()
    capsys.readouterr()
    _register_qualify_and_human_node(database, legacy, node_id="no_change_confirmation")

    assert (
        _lifecycle(
            database,
            "approve",
            "--run-id",
            "run-stage1",
            "--node-id",
            "no_change_confirmation",
            "--reason",
            "operator decision",
        )
        == 0
    )

    assert "execution_selection_digest" not in _node_state(database, "no_change_confirmation")


def test_stage1_cancel_rejects_human_wait_without_worker_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _start_request(database, tmp_path)
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="plan_approval")

    assert (
        _lifecycle(
            database,
            "cancel",
            "--run-id",
            "run-stage1",
            "--node-id",
            "plan_approval",
        )
        != 0
    )
    assert "cannot cancel a human-waiting non-agent node" in capsys.readouterr().err


@pytest.mark.usefixtures("authenticated_decisions")
def test_stage1_rejects_human_wait_and_invalid_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = replace(
        _request(tmp_path),
        run=replace(_request(tmp_path).run, id=RunId("run-stage1-reject")),
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(bound_document(request)), encoding="utf-8")
    assert _start(database, request_path) == 0
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="plan_approval")

    assert (
        _lifecycle(
            database,
            "reject",
            "--run-id",
            "run-stage1-reject",
            "--node-id",
            "plan_approval",
            "--reason",
            "operator rejected the proposed plan",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    invalid_request = tmp_path / "invalid-request.json"
    invalid_request.write_bytes(b"\xff")
    assert _start(database, invalid_request) != 0
    assert "UTF-8" in capsys.readouterr().err


def _start_request(
    database: Path, tmp_path: Path, *, selection: ExecutionSelection | None = None
) -> Stage1RunRequest:
    request = _request(tmp_path)
    if selection is not None:
        request = replace(request, execution_selection=selection)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(bound_document(request)), encoding="utf-8")
    assert _start(database, request_path) == 0
    return request


def _node_state(database: Path, node_id: str) -> dict[str, object]:
    ledger = LedgerService.open(database)
    try:
        projection = ledger.read_projection(
            aggregate_type="runtime_node", aggregate_id=f"run-stage1:{node_id}"
        )
        assert projection is not None
        return dict(projection.state)
    finally:
        ledger.close()


def _start(database: Path, request_path: Path) -> int:
    return main(
        [
            "stage1",
            "start",
            "--database",
            str(database),
            "--owner",
            "stage1-cli",
            "--request",
            str(request_path),
        ]
    )


def _lifecycle(database: Path, command: str, *arguments: str) -> int:
    return main(
        ["stage1", command, "--database", str(database), "--owner", "stage1-cli", *arguments]
    )


def _register_qualify_and_human_node(
    database: Path, request: Stage1RunRequest, *, node_id: str
) -> None:
    ledger = LedgerService.open(database)
    try:
        runtime = CoordinatorRuntime(ledger, owner="stage1-cli")
        now = datetime.now(tz=UTC)
        qualify = _dispatch(request, node_id="qualify", dependencies=())
        epoch = runtime.register_node(
            dispatch=WorkflowNodeDispatch(qualify, request.manifest),
            now=now,
            heartbeat_window=timedelta(seconds=60),
        )
        runtime.complete_node(
            run_id=str(request.run.id),
            node_id="qualify",
            epoch=epoch.epoch,
            now=now,
        )
        _register_human_node_with_runtime(runtime, request, node_id=node_id, now=now)
    finally:
        ledger.close()


def _register_human_node(database: Path, request: Stage1RunRequest, *, node_id: str) -> None:
    ledger = LedgerService.open(database)
    try:
        _register_human_node_with_runtime(
            CoordinatorRuntime(ledger, owner="stage1-cli"),
            request,
            node_id=node_id,
            now=datetime.now(tz=UTC),
        )
    finally:
        ledger.close()


def _register_human_node_with_runtime(
    runtime: CoordinatorRuntime,
    request: Stage1RunRequest,
    *,
    node_id: str,
    now: datetime,
) -> None:
    dispatch = _dispatch(request, node_id=node_id, dependencies=("qualify",))
    epoch = runtime.register_node(
        dispatch=WorkflowNodeDispatch(dispatch, request.manifest),
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )
    runtime.await_human_node(
        run_id=str(request.run.id),
        node_id=node_id,
        epoch=epoch.epoch,
        now=now,
        reason="operator decision required",
    )


def _dispatch(
    request: Stage1RunRequest, *, node_id: str, dependencies: tuple[str, ...]
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id=str(request.run.id),
        node_id=node_id,
        attempt_id=f"{node_id}-0",
        repository_id=request.repository_id,
        repository_path=request.repository_path,
        workspace_path=request.workspace_path,
        base_revision=request.run.base_revision,
        command=("fixture",),
        expected_attempt_version=0,
        operation_id=f"{node_id}:{request.run.id}:0",
        dependencies=tuple((str(request.run.id), dependency) for dependency in dependencies),
        workflow_definition_id=request.manifest.id.value,
    )


def _request(tmp_path: Path) -> Stage1RunRequest:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    if not (repository / ".git").exists():
        for arguments in (
            ("init", "--initial-branch=main"),
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.invalid"),
        ):
            subprocess.run(("git", *arguments), cwd=repository, check=True, capture_output=True)
        (repository / "README").write_text("fixture\n")
        subprocess.run(("git", "add", "README"), cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ("git", "commit", "-m", "fixture"), cwd=repository, check=True, capture_output=True
        )
        base = declare_behavioral_policy(repository)
    else:
        base = subprocess.check_output(
            ("git", "rev-parse", "HEAD"), cwd=repository, text=True
        ).strip()
    manifest = stage1_work_manifest()
    work_item = WorkItem(
        id=WorkItemId("work-1"),
        work_kind=WorkKind.ISSUE,
        source_provider="github",
        external_reference="Mathews-Tom/Enginery#1",
        source_snapshot_reference="issue:1@issue-revision-1",
        title="Bounded change",
        objective="Change one bounded behavior.",
        acceptance_criteria=("observable result",),
        constraints=("retain evidence",),
        risk_class=RiskClass.LOW,
        repository_targets=("Mathews-Tom/Enginery",),
        dependencies=(),
        state=WorkItemState.QUALIFYING,
    )
    snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision="issue-revision-1")
    return Stage1RunRequest(
        behavioral_checks=fixture_behavioral_checks(),
        runner=RunnerSelection.trusted_local(),
        run=Run(
            id=RunId("run-stage1"),
            work_item_id=work_item.id,
            work_item_snapshot_digest=work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="Mathews-Tom/Enginery",
            base_revision=base,
            policy_set_version="policy-v1",
            adapter_versions={},
            adapter_fingerprints={},
            capability_lock_digest=Digest.of_bytes(b"capability-lock"),
            environment_manifest_digest=Digest.of_bytes(b"environment"),
            configuration_snapshot_digest=Digest.of_bytes(b"configuration"),
            state=RunState.CREATED,
        ),
        work_snapshot=snapshot,
        manifest=manifest,
        repository_id="Mathews-Tom/Enginery",
        repository_path=repository.resolve(),
        workspace_path=(tmp_path / "workspace").resolve(),
        execution_selection=selection_for("omp"),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/Enginery",
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            harness_provider="omp",
            harness_credential_reference="test-omp-keyring",
            harness_executable="omp",
            artifact_root=(tmp_path / "artifacts").resolve(),
        ),
        base_branch="main",
        head_branch="enginery/stage1",
        validation_commands=(("uv", "run", "pytest", "-q"),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:run-stage1"),
            time_budget_seconds=60,
            cost_budget=None,
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
    )


def test_harness_for_selects_the_configured_provider_without_a_fallback(tmp_path: Path) -> None:
    from enginery.adapters.claude_code import ClaudeCodeHarness
    from enginery.adapters.omp import OmpHarness
    from enginery.cli.stage1_composition import harness_for

    omp_config = replace(_request(tmp_path).execution_configuration, harness_provider="omp")
    claude_config = replace(
        _request(tmp_path).execution_configuration, harness_provider="claude-code"
    )

    assert isinstance(harness_for(omp_config), OmpHarness)
    assert isinstance(harness_for(claude_config), ClaudeCodeHarness)


def test_harness_for_rejects_an_unconfigured_provider_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    from enginery.cli.stage1_composition import harness_for

    configuration = replace(
        _request(tmp_path).execution_configuration, harness_provider="unknown-harness"
    )

    with pytest.raises(InvalidInputError, match="harness_provider"):
        harness_for(configuration)


def _readonly_request(
    tmp_path: Path, *, selection: ExecutionSelection, executable: str, blocked: Path
) -> Path:
    request = _request(tmp_path)
    request = replace(
        request,
        execution_selection=selection,
        execution_configuration=replace(
            request.execution_configuration,
            harness_executable=executable,
            artifact_root=blocked / "artifacts",
        ),
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(bound_document(request)), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("executable", "expected_action", "expected_hold"),
    [
        ("/nonexistent/enginery-host", "hold", "host_fingerprint_drift"),
        ("host-script", "qualify", None),
    ],
    ids=["unobservable-host", "tested-host"],
)
def test_read_only_diagnostics_never_create_harness_state_and_never_crash_on_the_host(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    executable: str,
    expected_action: str,
    expected_hold: str | None,
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the artifact root's parent would be", encoding="utf-8")
    if executable == "host-script":
        script = tmp_path / "host-script"
        script.write_text("#!/bin/sh\necho 'omp v18.10.3'\n", encoding="utf-8")
        script.chmod(0o755)
        executable = str(script)
    pinned = selection_for("omp", model="openai/gpt-5.2")
    request_path = _readonly_request(
        tmp_path, selection=pinned, executable=executable, blocked=blocker
    )
    database = tmp_path / "ledger.db"
    assert _start(database, request_path) == 0
    capsys.readouterr()

    assert _lifecycle(database, "watch", "--run-id", "run-stage1") == 0

    watched = json.loads(capsys.readouterr().out)
    assert watched["next_action"] == expected_action
    if expected_hold is None:
        assert watched["hold"] is None
    else:
        assert watched["hold"]["reason"] == expected_hold
        assert watched["hold"]["details"]["installed_executable_version"] == "unobservable"
    assert blocker.read_text(encoding="utf-8").startswith("a file")
    assert not (tmp_path / "artifacts").exists()

    capsys.readouterr()
    assert _lifecycle(database, "evidence", "--run-id", "run-stage1") == 0
    evidence = json.loads(capsys.readouterr().out)
    if expected_hold is None:
        assert evidence["hold"] is None
    else:
        assert evidence["hold"]["reason"] == expected_hold
        assert evidence["execution"]["holds"] == [evidence["hold"]]
    assert evidence["execution"]["selected"]["model"] == "openai/gpt-5.2"
    assert blocker.read_text(encoding="utf-8").startswith("a file")
    assert not (tmp_path / "artifacts").exists()


def test_a_host_default_run_is_never_probed_by_read_only_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    request_path = _readonly_request(
        tmp_path,
        selection=selection_for("omp"),
        executable="/nonexistent/enginery-host",
        blocked=blocker,
    )
    database = tmp_path / "ledger.db"
    assert _start(database, request_path) == 0
    capsys.readouterr()

    assert _lifecycle(database, "watch", "--run-id", "run-stage1") == 0

    watched = json.loads(capsys.readouterr().out)
    assert watched["next_action"] == "qualify"
    assert watched["hold"] is None


def test_watch_reports_a_new_runs_absent_cost_budget_as_unenforced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(bound_document(_request(tmp_path))), encoding="utf-8")
    assert _start(database, request_path) == 0
    capsys.readouterr()

    assert _lifecycle(database, "watch", "--run-id", "run-stage1") == 0

    assert json.loads(capsys.readouterr().out)["cost_budget"] == {"value": None, "enforced": False}


def test_watch_never_presents_a_legacy_carried_dollar_number_as_enforcement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = _request(tmp_path)
    legacy = replace(
        request,
        runner=LEGACY_RUNNER_SELECTION,
        implementation=replace(request.implementation, cost_budget=Decimal("5.0")),
    )
    # A run persisted before trust modes carries its recorded number; it is read, never rewritten.
    ledger = LedgerService.open(database)
    try:
        CoordinatorRuntime(ledger, owner="stage1-cli").register_run(
            run_id=str(legacy.run.id),
            initial_state=legacy.initial_state(),
            now=datetime.now(tz=UTC),
            heartbeat_window=timedelta(seconds=60),
        )
    finally:
        ledger.close()

    assert _lifecycle(database, "watch", "--run-id", "run-stage1") == 0
    watched = json.loads(capsys.readouterr().out)
    assert _lifecycle(database, "evidence", "--run-id", "run-stage1") == 0
    evidence = json.loads(capsys.readouterr().out)

    assert watched["cost_budget"] == {"value": "5.0", "enforced": False}
    assert evidence["request_digest"] == str(legacy.digest)


@pytest.mark.usefixtures("authenticated_decisions")
def test_factory_explicit_stage1_ledger_refuses_unsupported_requeue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "operator's ledger.db"
    request = _start_request(database, tmp_path)
    capsys.readouterr()
    _register_qualify_and_human_node(database, request, node_id="no_change_confirmation")
    scope = [
        "--project",
        str(request.repository_path),
        "--database",
        str(database),
        "--owner",
        "stage1-cli",
    ]
    assert main(["factory", "review", "run-stage1", *scope, "--json"]) == 0
    report_text, challenge_line = capsys.readouterr().out.strip().rsplit("\n", 1)
    report = json.loads(report_text)
    assert report["challenge_issued"] is True
    challenge = challenge_line.removeprefix("Decision challenge: ")
    assert challenge_line != challenge
    assert (
        main(
            [
                "factory",
                "decide",
                "run-stage1",
                *scope,
                "--action",
                "human_wait.no_change_confirmation.requeue",
                "--subject-digest",
                report["subject_digest"],
                "--challenge",
                challenge,
                "--reason",
                "reconsider the implementation",
                "--json",
            ]
        )
        == 2
    )
    assert "human_wait_requeue_unsupported" in capsys.readouterr().err
    assert _node_state(database, "no_change_confirmation")["status"] == "awaiting_human"
    with LedgerService.open(database) as ledger:
        row = ledger.connection.execute(
            "SELECT consumed_at FROM human_decision_challenges"
        ).fetchone()
        assert row["consumed_at"] is None
