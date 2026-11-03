"""Consumer regressions and disposable, network-free scorecard smoke fixtures."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.execution_receipt import EffectiveControl, ExecutionReceipt
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.ids import (
    NodeAttemptId,
    NodeId,
    OperationId,
    OutcomeId,
    RunId,
    WorkflowDefinitionId,
    WorkItemId,
)
from enginery.domain.node_attempt import NodeAttempt, NodeAttemptState
from enginery.domain.outcome import OutcomeKind
from enginery.domain.run import Run, RunState
from enginery.domain.runner import LEGACY_RUNNER_SELECTION, RunnerSelection
from enginery.domain.serialization import node_attempt_to_dict
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.run_intent import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
)
from tests.support.execution import selection_for

_RUN = "run-scorecard"
_NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
_MODEL = "openai/gpt-5.2"
_BASE = "a" * 40
_HEAD = "b" * 40


def _request(root: Path) -> Stage1RunRequest:
    """Bind valid request data without touching Git, executable hosts or credentials."""
    manifest = stage1_work_manifest()
    work_item = WorkItem(
        id=WorkItemId("work-scorecard"),
        work_kind=WorkKind.ISSUE,
        source_provider="github",
        external_reference="example/project#42",
        source_snapshot_reference="issue:42@revision-3",
        title="One bounded change",
        objective="An observable result",
        acceptance_criteria=("The result is observable",),
        constraints=("Retain source evidence",),
        risk_class=RiskClass.LOW,
        repository_targets=("example/project",),
        dependencies=(),
        state=WorkItemState.QUALIFYING,
    )
    return Stage1RunRequest(
        run=Run(
            id=RunId(_RUN),
            work_item_id=work_item.id,
            work_item_snapshot_digest=work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="example/project",
            base_revision=_BASE,
            policy_set_version="policy-v1",
            adapter_versions={},
            adapter_fingerprints={},
            capability_lock_digest=Digest.of_bytes(b"capability-lock"),
            environment_manifest_digest=Digest.of_bytes(b"environment"),
            configuration_snapshot_digest=Digest.of_bytes(b"configuration"),
            state=RunState.CREATED,
        ),
        work_snapshot=WorkLedgerSnapshot(work_item=work_item, source_revision="revision-3"),
        manifest=manifest,
        repository_id="example/project",
        repository_path=(root / "absent-repository").resolve(),
        workspace_path=(root / "absent-workspace").resolve(),
        base_branch="main",
        head_branch="enginery/scorecard",
        validation_commands=(("pytest", "-q"),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:scorecard"),
            time_budget_seconds=60,
            cost_budget=None,
            permitted_capabilities=("git",),
            evidence_requirements=("redacted transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="example/project",
            github_credential_reference="fixture-github-profile",
            github_executable="absent-gh",
            harness_provider="omp",
            harness_credential_reference="fixture-harness-profile",
            harness_executable="absent-omp",
            artifact_root=(root / "absent-artifacts").resolve(),
        ),
        execution_selection=selection_for("omp", model=_MODEL, effort="high"),
        runner=RunnerSelection.trusted_local(),
    )


def _persist(
    ledger: LedgerService,
    aggregate_type: str,
    aggregate_id: str,
    state: dict[str, object],
    event_type: str,
) -> None:
    projection = ledger.read_projection(aggregate_type=aggregate_type, aggregate_id=aggregate_id)
    version = 0 if projection is None else projection.aggregate_version
    ledger.append(
        AppendCommand(
            correlation_id=f"fixture:{aggregate_type}:{aggregate_id}:{version}",
            events=(
                EventWrite(
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    expected_version=version,
                    event_type=event_type,
                    schema_version=1,
                    payload=state,
                ),
            ),
        )
    )


def _node(
    ledger: LedgerService, node_id: str, status: str, *, generation: int = 1, **extra: object
) -> None:
    _persist(
        ledger,
        "runtime_node",
        f"{_RUN}:{node_id}",
        {
            "run_id": _RUN,
            "node_id": node_id,
            "attempt_id": f"{node_id}-{generation}",
            "status": status,
            **extra,
        },
        "runtime_node.deterministic_human_wait"
        if status == "awaiting_human"
        else "runtime_node.deterministic_completed",
    )


def seed_summary_fixture(database: Path, scenario: str) -> dict[str, object]:
    """Seed success, held or incomplete source facts for tests and real CLI smoke.

    This deliberately never dispatches a worker or reads repository/artifact paths.
    Returned facts are independent of the summary consumer, suitable for comparing
    a subprocess's JSON output with its disposable source records.
    """
    if scenario not in {"success", "held", "incomplete"}:
        raise ValueError(f"unknown scorecard fixture scenario: {scenario}")
    request = _request(database.parent)
    with LedgerService.open(database) as ledger:
        _persist(ledger, "run", _RUN, request.initial_state(), "run.created")
        _node(ledger, "qualify", "passed", generation=0)
        _node(ledger, "implement", "queued", generation=0)
        if scenario != "incomplete":
            legacy = NodeAttempt(
                id=NodeAttemptId("implement-0"),
                run_id=RunId(_RUN),
                node_id=NodeId("implement"),
                attempt_number=1,
                actor="omp",
                input_digest=request.digest,
                state=NodeAttemptState.FAILED,
                started_at=_NOW,
                completed_at=_NOW + timedelta(seconds=12),
                duration_seconds=12.0,
                cost_amount=0.25,
            )
            _persist(
                ledger,
                "node_attempt",
                "implement-0",
                node_attempt_to_dict(legacy),
                "node_attempt.failed",
            )
            _node(ledger, "implement", "failed", generation=0, failure_reason="repair needed")
            repair_operation = str(
                OperationId.derive(
                    run_id=request.run.id,
                    node_id=NodeId("implement"),
                    side_effect_kind="repair_implementation",
                    target_scope=request.repository_id,
                    ordinal=1,
                )
            )
            receipt = ExecutionReceipt(
                selection_digest=request.execution_selection.digest,
                harness="omp",
                submitted_arguments=("--model", _MODEL, "--thinking", "high"),
                effective_model=EffectiveControl.observed(
                    _MODEL, source="message_end"
                ).worker_reported(),
                effective_effort=EffectiveControl.unknown(reason="host did not report effort"),
            )
            for status, event_type in (
                ("running", "runtime_node.started"),
                ("awaiting_human", "runtime_node.human_wait_entered"),
                ("running", "runtime_node.human_wait_resumed"),
            ):
                _persist(
                    ledger,
                    "runtime_node",
                    f"{_RUN}:implement",
                    {
                        "run_id": _RUN,
                        "node_id": "implement",
                        "attempt_id": "implement-1",
                        "status": status,
                        "operation_id": repair_operation,
                    },
                    event_type,
                )
            _persist(
                ledger,
                "node_attempt",
                "implement-1",
                {
                    "run_id": _RUN,
                    "node_id": "implement",
                    "attempt_id": "implement-1",
                    "terminal_result": "passed",
                    "operation_id": repair_operation,
                    "result": {
                        "harness_status": "succeeded",
                        "execution_receipt": receipt.to_state(),
                    },
                    "artifact_references": [],
                },
                "node_attempt.result_ingested",
            )
            _node(ledger, "implement", "passed", operation_id=repair_operation)
            _node(ledger, "validate", "passed", validated_revision=_HEAD)
            _node(ledger, "review", "passed", review_outcome="approved")
            if scenario == "success":
                _node(
                    ledger,
                    "open_pr",
                    "passed",
                    pull_request_number=42,
                    head_revision=_HEAD,
                    base_revision=_BASE,
                    published_revision=_HEAD,
                )
                _node(ledger, "wait_for_ci", "passed", pull_request_outcome="merge_ready")
                _node(
                    ledger,
                    "verify",
                    "passed",
                    pull_request_outcome="merge_ready",
                    head_revision=_HEAD,
                    evidence_digest=str(Digest.of_bytes(b"verified-current-head")),
                )
            else:
                _node(
                    ledger,
                    "open_pr",
                    "awaiting_human",
                    reason="publication_policy_denied",
                    repair="Review the publication hold; manual delivery was recorded separately",
                )
                _persist(
                    ledger,
                    "runtime_node",
                    f"{_RUN}:review",
                    {
                        "run_id": _RUN,
                        "node_id": "review",
                        "attempt_id": "review-1",
                        "status": "passed",
                        "operator_decision": "approved",
                        "reason": "Operator reviewed candidate before manual handoff",
                    },
                    "runtime_node.human_wait_resolved",
                )
            outcomes = OutcomeCaptureService(ledger=ledger)
            observation = outcomes.register_pending(
                work_item_id=request.run.work_item_id,
                run_id=request.run.id,
                kind=OutcomeKind.MERGE_RESULT,
                subject_reference="42" if scenario == "success" else "99",
                opened_at=_NOW,
            )
            if scenario == "held":
                outcomes.capture(
                    observation.id,
                    outcome_id=OutcomeId("outcome-manual-merge"),
                    kind=OutcomeKind.PR_ACCEPTED,
                    observed_at=_NOW + timedelta(days=1),
                    detail={
                        "pull_request_number": 99,
                        "merged": True,
                        "delivery": "manual_handoff",
                    },
                )
    return {
        "run_id": _RUN,
        "work_item_id": str(request.run.work_item_id),
        "request_digest": str(request.digest),
        "workflow_definition_digest": str(request.manifest.content_digest),
        "work_item_snapshot_digest": str(request.run.work_item_snapshot_digest),
        "base_revision": _BASE,
        "head_revision": _HEAD,
        "source_revision": "revision-3",
        "scenario": scenario,
        "implementation_attempt_count": 1 if scenario == "incomplete" else 2,
        "native_completed_task_count": int(scenario == "success"),
    }


def ledger_snapshot(database: Path) -> tuple[str, ...]:
    """Snapshot every durable table, not WAL/SHM file creation or byte layout."""
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        return tuple(connection.iterdump())


def _summary(database: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    exit_code = main(["outcome", "summary", "--database", str(database), "--run-id", _RUN])
    output = capsys.readouterr()
    assert exit_code == 0, output.err
    assert output.err == ""
    payload: dict[str, Any] = json.loads(output.out)
    return payload


def test_summary_reconciles_success_with_source_identity_and_publication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    facts = seed_summary_fixture(database, "success")

    payload = _summary(database, capsys)

    assert payload["schema_version"] == 1
    for key in (
        "run_id",
        "work_item_id",
        "request_digest",
        "workflow_definition_digest",
        "work_item_snapshot_digest",
        "base_revision",
        "source_revision",
    ):
        assert payload["run"][key] == facts[key]
    assert payload["run"]["repository_id"] == "example/project"
    assert payload["run"]["base_branch"] == "main"
    assert payload["run"]["head_branch"] == "enginery/scorecard"
    assert payload["accounting"]["task_count"] == 1
    assert payload["accounting"]["native_completed_task_count"] == 1
    assert payload["native_publication"]["state"] == "published"
    publication = payload["native_publication"]["publications"][0]
    assert publication["pull_request_number"] == 42
    assert publication["head_revision"] == _HEAD
    assert payload["downstream"]["outcomes"] == []
    assert payload["downstream"]["observations"][0]["data"]["state"] == "pending"
    evidence = payload["run"]["evidence"][0]
    assert evidence["aggregate_type"] == "run"
    assert evidence["aggregate_id"] == _RUN
    assert evidence["event_type"] == "run.created"
    assert evidence["commit_seq"] >= 1


def test_summary_keeps_manual_merge_separate_from_native_publication_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "held")

    payload = _summary(database, capsys)

    assert payload["accounting"]["task_count"] == 1
    assert payload["accounting"]["native_completed_task_count"] == 0
    assert payload["native_publication"]["state"] == "not_recorded"
    assert payload["native_publication"]["publications"] == []
    assert any(
        hold["reason"] == "publication_policy_denied" for hold in payload["lifecycle"]["holds"]
    )
    outcome = payload["downstream"]["outcomes"][0]["data"]
    assert outcome["kind"] == "pr_accepted"
    assert outcome["detail"]["delivery"] == "manual_handoff"
    assert outcome["detail"]["pull_request_number"] == 99
    assert payload["assistance"]["recorded_decision_count"] == 1
    effort = payload["assistance"]["total_human_effort"]
    assert effort["state"] == "unknown"
    assert effort["value"] is None


def test_summary_counts_repair_attempts_not_resumes_as_completed_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")

    payload = _summary(database, capsys)

    accounting = payload["accounting"]
    assert accounting["task_count"] == accounting["native_completed_task_count"] == 1
    assert accounting["implementation_attempt_count"] == 2
    assert accounting["recorded_repair_count"] == 1
    assert any(
        failure["attempt_id"] == "implement-0" for failure in payload["lifecycle"]["failures"]
    )
    assert payload["lifecycle"]["current_failure_node_ids"] == []
    attempts = payload["attempts"]
    assert accounting["attempt_count"] == len(attempts)
    assert len({attempt["attempt_id"] for attempt in attempts}) == len(attempts)
    assert {attempt["attempt_id"] for attempt in attempts if attempt["node_id"] == "implement"} == {
        "implement-0",
        "implement-1",
    }
    assert any(
        event["event_type"] == "runtime_node.human_wait_resumed"
        for event in payload["lifecycle"]["history"]
    )


def test_summary_reports_partial_legacy_measurements_and_observed_receipt_controls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")

    measurements = _summary(database, capsys)["measurements"]

    for name, field, value in (("timing", "duration_seconds", 12.0), ("cost", "cost_amount", 0.25)):
        measurement = measurements[name]
        assert measurement["state"] == "partial"
        assert measurement["coverage"]["measured_attempts"] == 1
        assert measurement["coverage"]["attempts"] > 1
        assert measurement["samples"][0]["attempt_id"] == "implement-0"
        assert measurement["samples"][0][field] == value
        assert measurement["samples"][0]["evidence"]["aggregate_type"] == "node_attempt"
    assert measurements["timing"]["total_duration_seconds"] is None
    assert measurements["cost"]["total_cost_amount"] is None
    assert measurements["usage"]["state"] == "unknown"
    controls = measurements["effective_controls"]
    assert controls["coverage"] == {"implementation_attempts": 2, "receipts": 1}
    receipt = controls["receipts"][0]
    assert receipt["attempt_id"] == "implement-1"
    assert receipt["effective_model"]["state"] == "observed"
    assert receipt["effective_model"]["value"] == _MODEL
    assert receipt["effective_model"]["source"] == "message_end"
    assert receipt["effective_model"]["reported_by"] == "worker"
    assert receipt["effective_effort"]["state"] == "unknown"
    assert receipt["effective_effort"]["value"] is None
    assert receipt["evidence"]["aggregate_id"] == "implement-1"


def test_summary_missing_measurements_are_unknown_not_requested_values_or_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "incomplete")

    payload = _summary(database, capsys)

    assert payload["accounting"]["native_completed_task_count"] == 0
    assert payload["accounting"]["implementation_attempt_count"] == 1
    assert payload["downstream"]["observations"] == []
    assert payload["downstream"]["outcomes"] == []
    assert payload["downstream"]["missing_observation_state"] == "unknown"
    assert payload["assistance"]["recorded_decision_count"] == 0
    assert payload["assistance"]["total_human_effort"]["value"] is None
    for name in ("timing", "usage", "cost", "effective_controls"):
        assert payload["measurements"][name]["state"] == "unknown"
    assert payload["measurements"]["timing"]["samples"] == []
    assert payload["measurements"]["cost"]["samples"] == []
    controls = payload["measurements"]["effective_controls"]
    assert controls["coverage"] == {"implementation_attempts": 1, "receipts": 0}
    assert controls["receipts"] == []
    assert not (tmp_path / "absent-repository").exists()
    assert not (tmp_path / "absent-artifacts").exists()


@pytest.mark.parametrize("scenario", ["success", "held", "incomplete"])
def test_summary_leaves_all_ledger_contents_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], scenario: str
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, scenario)
    before = ledger_snapshot(database)

    first = _summary(database, capsys)
    second = _summary(database, capsys)

    assert first == second
    assert ledger_snapshot(database) == before


def test_summary_missing_database_does_not_create_file_or_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "missing-parent" / "missing.db"

    result = main(["outcome", "summary", "--database", str(database), "--run-id", _RUN])

    assert result == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert str(database) in output.err
    assert not database.parent.exists()


def test_summary_unknown_run_reports_named_error_without_mutating_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "incomplete")
    before = ledger_snapshot(database)

    result = main(["outcome", "summary", "--database", str(database), "--run-id", "absent-run"])

    assert result == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "absent-run" in output.err
    assert ledger_snapshot(database) == before


@pytest.mark.parametrize("malformation", ["receipt", "timestamp", "cost", "event_json"])
def test_summary_malformed_evidence_fails_visibly_with_source_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], malformation: str
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")
    with LedgerService.open(database) as ledger:
        if malformation == "event_json":
            # Simulate damaged retained bytes, rather than a new telemetry producer.
            ledger.connection.execute(
                "UPDATE events SET payload = ? WHERE aggregate_type = ? AND aggregate_id = ?",
                ("{not-json", "node_attempt", "implement-1"),
            )
            ledger.connection.commit()
        elif malformation == "receipt":
            _persist(
                ledger,
                "node_attempt",
                "implement-1",
                {
                    "run_id": _RUN,
                    "node_id": "implement",
                    "attempt_id": "implement-1",
                    "terminal_result": "passed",
                    "result": {"execution_receipt": {"schema_version": 99}},
                },
                "node_attempt.result_ingested",
            )
        else:
            legacy = NodeAttempt(
                id=NodeAttemptId("implement-0"),
                run_id=RunId(_RUN),
                node_id=NodeId("implement"),
                attempt_number=1,
                actor="omp",
                input_digest=Digest.of_bytes(b"fixture"),
                state=NodeAttemptState.FAILED,
            )
            state = node_attempt_to_dict(replace(legacy, duration_seconds=12.0))
            data = state["data"]
            assert isinstance(data, dict)
            data["started_at" if malformation == "timestamp" else "cost_amount"] = (
                "not-a-timestamp" if malformation == "timestamp" else "not-a-number"
            )
            _persist(ledger, "node_attempt", "implement-0", state, "node_attempt.failed")
    before = ledger_snapshot(database)

    result = main(["outcome", "summary", "--database", str(database), "--run-id", _RUN])

    assert result == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "malformed outcome summary evidence" in output.err
    assert "implement-" in output.err
    assert "Traceback" not in output.err
    assert ledger_snapshot(database) == before


@pytest.mark.parametrize("mismatch", ["generation", "head", "outcome"])
def test_summary_does_not_count_stale_or_failed_verification_as_native_completion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mismatch: str
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")
    with LedgerService.open(database) as ledger:
        _node(
            ledger,
            "verify",
            "passed",
            generation=0 if mismatch == "generation" else 1,
            head_revision="c" * 40 if mismatch == "head" else _HEAD,
            pull_request_outcome="blocked" if mismatch == "outcome" else "merge_ready",
            evidence_digest=str(Digest.of_bytes(b"another-verification")),
        )

    payload = _summary(database, capsys)

    assert payload["accounting"]["native_completed_task_count"] == 0
    assert payload["native_publication"]["state"] == "published"


def test_summary_preserves_historical_request_digest_and_unknown_controls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    request = replace(
        _request(tmp_path),
        execution_selection=ExecutionSelection.legacy_host_default("omp"),
        runner=LEGACY_RUNNER_SELECTION,
    )
    state = request.initial_state()
    assert "execution_selection" not in state
    assert "runner" not in state
    with LedgerService.open(database) as ledger:
        _persist(ledger, "run", _RUN, state, "run.created")
        _node(ledger, "implement", "queued", generation=0)

    payload = _summary(database, capsys)

    assert payload["run"]["request_digest"] == str(request.digest)
    assert payload["accounting"]["task_count"] == 1
    assert payload["measurements"]["effective_controls"]["state"] == "unknown"


def test_summary_is_scoped_to_one_run_not_similar_run_identifiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "incomplete")
    other_run = f"{_RUN}-other"
    with LedgerService.open(database) as ledger:
        _persist(
            ledger,
            "runtime_node",
            f"{other_run}:implement",
            {
                "run_id": other_run,
                "node_id": "implement",
                "attempt_id": "other-implement-0",
                "status": "failed",
                "reason": "another run failed",
            },
            "runtime_node.completed",
        )
        service = OutcomeCaptureService(ledger=ledger)
        observation = service.register_pending(
            work_item_id=WorkItemId("other-work"),
            run_id=RunId(other_run),
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="100",
            opened_at=_NOW,
        )
        service.capture(
            observation.id,
            outcome_id=OutcomeId("other-outcome"),
            kind=OutcomeKind.PR_ACCEPTED,
            observed_at=_NOW + timedelta(days=1),
        )

    payload = _summary(database, capsys)

    assert payload["accounting"]["implementation_attempt_count"] == 1
    assert payload["lifecycle"]["failures"] == []
    assert payload["downstream"]["observations"] == []
    assert payload["downstream"]["outcomes"] == []


def test_summary_preserves_explicit_zero_measurements_as_recorded_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")
    attempt = NodeAttempt(
        id=NodeAttemptId("implement-0"),
        run_id=RunId(_RUN),
        node_id=NodeId("implement"),
        attempt_number=1,
        actor="omp",
        input_digest=Digest.of_bytes(b"zero-sample"),
        state=NodeAttemptState.FAILED,
        duration_seconds=0.0,
        cost_amount=0.0,
    )
    with LedgerService.open(database) as ledger:
        _persist(
            ledger,
            "node_attempt",
            "implement-0",
            node_attempt_to_dict(attempt),
            "node_attempt.failed",
        )

    measurements = _summary(database, capsys)["measurements"]

    assert measurements["timing"]["samples"][0]["duration_seconds"] == 0.0
    assert measurements["cost"]["samples"][0]["cost_amount"] == 0.0
    assert measurements["timing"]["coverage"]["measured_attempts"] == 1
    assert measurements["cost"]["coverage"]["measured_attempts"] == 1


def assert_summary_source_facts(
    database: Path, facts: dict[str, object], payload: dict[str, Any]
) -> None:
    """Compare a CLI document with fixture facts and every cited retained event."""
    for key in (
        "run_id",
        "work_item_id",
        "request_digest",
        "workflow_definition_digest",
        "work_item_snapshot_digest",
        "base_revision",
        "source_revision",
    ):
        assert payload["run"][key] == facts[key]
    assert payload["accounting"]["task_count"] == 1
    for key in ("native_completed_task_count", "implementation_attempt_count"):
        assert payload["accounting"][key] == facts[key]
    if facts["scenario"] == "held":
        assert payload["native_publication"]["state"] == "not_recorded"
        assert (
            payload["downstream"]["outcomes"][0]["data"]["detail"]["delivery"] == "manual_handoff"
        )
    elif facts["scenario"] == "success":
        assert (
            payload["native_publication"]["publications"][0]["head_revision"]
            == facts["head_revision"]
        )
    else:
        assert payload["measurements"]["effective_controls"]["receipts"] == []
    references: list[dict[str, Any]] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            if {
                "aggregate_type",
                "aggregate_id",
                "commit_seq",
                "aggregate_version",
            } <= value.keys():
                references.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(payload)
    assert references
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for reference in references:
            row = connection.execute(
                "SELECT aggregate_type, aggregate_id, aggregate_version, event_type, recorded_at "
                "FROM events WHERE commit_seq = ?",
                (reference["commit_seq"],),
            ).fetchone()
            assert row is not None
            for key in (
                "aggregate_type",
                "aggregate_id",
                "aggregate_version",
                "event_type",
                "recorded_at",
            ):
                assert row[key] == reference[key]


@pytest.mark.parametrize("scenario", ["success", "held", "incomplete"])
def test_summary_provenance_reconciles_with_exact_retained_source_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], scenario: str
) -> None:
    database = tmp_path / "ledger.db"
    facts = seed_summary_fixture(database, scenario)

    payload = _summary(database, capsys)

    assert_summary_source_facts(database, facts, payload)


@pytest.mark.parametrize("aggregate_type", ["run", "runtime_node", "observation"])
def test_summary_malformed_projection_json_is_a_visible_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], aggregate_type: str
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")
    with LedgerService.open(database) as ledger:
        ledger.connection.execute(
            "UPDATE projections SET state_json = ? WHERE aggregate_type = ?",
            ("{not-json", aggregate_type),
        )
        ledger.connection.commit()
    before = ledger_snapshot(database)

    result = main(["outcome", "summary", "--database", str(database), "--run-id", _RUN])

    assert result != 0
    output = capsys.readouterr()
    assert output.out == ""
    assert _RUN in output.err
    assert "Traceback" not in output.err
    assert ledger_snapshot(database) == before


def test_summary_does_not_infer_a_repair_from_an_additional_attempt_suffix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    seed_summary_fixture(database, "success")
    with LedgerService.open(database) as ledger:
        _node(
            ledger,
            "implement",
            "queued",
            generation=2,
            operation_id="unclassified-operation",
        )

    accounting = _summary(database, capsys)["accounting"]

    assert accounting["task_count"] == 1
    assert accounting["implementation_attempt_count"] == 3
    assert accounting["recorded_repair_count"] == 1
    assert "implement-2" in accounting["unclassified_implementation_attempt_ids"]
