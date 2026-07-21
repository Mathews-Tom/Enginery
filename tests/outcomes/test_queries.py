"""Tests for enginery.evaluation.queries: intervention and failure history."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.workflows.test_stage1_runtime import _request

from enginery.domain.ids import RunId
from enginery.engine.runtime import (
    RUNTIME_NODE_AGGREGATE_TYPE,
    CoordinatorRuntime,
    WorkflowNodeDispatch,
)
from enginery.evaluation.queries import list_failures, list_interventions
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest

_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def test_list_interventions_reads_a_recorded_human_decision(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    qualification = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=qualification, now=_NOW, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=_NOW)
    approval = WorkflowNodeDispatch(
        replace(
            qualification.request,
            node_id="plan_approval",
            attempt_id="attempt-approval",
            operation_id="operation-approval",
            dependencies=(("run-1", "qualify"),),
        ),
        issue_to_pr_manifest(),
    )
    runtime.register_node(dispatch=approval, now=_NOW, heartbeat_window=timedelta(seconds=60))
    runtime.await_human_node(
        run_id="run-1",
        node_id="plan_approval",
        epoch=epoch.epoch,
        now=_NOW,
        reason="approval required",
    )
    runtime.resolve_human_wait(
        run_id="run-1",
        node_id="plan_approval",
        epoch=epoch.epoch,
        now=_NOW,
        outcome="passed",
        extra={"operator_decision": "approved", "reason": "reviewed and correct"},
    )

    interventions = list_interventions(
        ledger_service, run_id=RunId("run-1"), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
    )

    assert len(interventions) == 1
    assert interventions[0].node_id == "plan_approval"
    assert interventions[0].decision == "approved"
    assert interventions[0].reason == "reviewed and correct"
    assert interventions[0].status == "passed"


def test_list_interventions_excludes_nodes_without_an_operator_decision(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    qualification = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=qualification, now=_NOW, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=_NOW)

    interventions = list_interventions(
        ledger_service, run_id=RunId("run-1"), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
    )

    assert interventions == ()


def test_list_failures_reads_a_failed_node(ledger_service: LedgerService, tmp_path: Path) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    qualification = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=qualification, now=_NOW, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(
        run_id="run-1",
        node_id="qualify",
        epoch=epoch.epoch,
        now=_NOW,
        outcome="failed",
        extra={"failure_reason": "issue no longer qualifies"},
    )

    failures = list_failures(
        ledger_service, run_id=RunId("run-1"), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
    )

    assert len(failures) == 1
    assert failures[0].node_id == "qualify"
    assert failures[0].detail["failure_reason"] == "issue no longer qualifies"


def test_list_failures_excludes_passed_nodes(ledger_service: LedgerService, tmp_path: Path) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    qualification = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=qualification, now=_NOW, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=_NOW)

    failures = list_failures(
        ledger_service, run_id=RunId("run-1"), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
    )

    assert failures == ()


def test_queries_are_scoped_to_the_requested_run(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    other_repository = tmp_path / "other-repository"
    other_repository.mkdir()
    first = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    second = WorkflowNodeDispatch(
        replace(
            _request(other_repository),
            run_id="run-2",
            operation_id="operation-run-2",
        ),
        issue_to_pr_manifest(),
    )
    epoch_one = runtime.register_node(
        dispatch=first, now=_NOW, heartbeat_window=timedelta(seconds=60)
    )
    epoch_two = runtime.register_node(
        dispatch=second, now=_NOW, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(
        run_id="run-1", node_id="qualify", epoch=epoch_one.epoch, now=_NOW, outcome="failed"
    )
    runtime.complete_node(
        run_id="run-2", node_id="qualify", epoch=epoch_two.epoch, now=_NOW, outcome="failed"
    )

    run_one_failures = list_failures(
        ledger_service, run_id=RunId("run-1"), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
    )
    run_two_failures = list_failures(
        ledger_service, run_id=RunId("run-2"), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
    )

    assert len(run_one_failures) == 1
    assert len(run_two_failures) == 1
    assert run_one_failures[0].run_id == "run-1"
    assert run_two_failures[0].run_id == "run-2"
