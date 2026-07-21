"""Dogfooding proof: this repository's own Stage 1 workflow, driven to
merge readiness, produces an outcome observation queryable through the
``enginery outcome`` CLI -- the same read path an operator would use.

Uses local fixture adapters (matching every other Stage 1 CLI test in
this suite), not live GitHub/OMP credentials: that live-provider proof is
a separate, already-established opt-in category (see
``tests/provider_smoke``). This test's job is narrower and specific to
M14a: prove the coordinator-owned Stage 1 progression service that the
real CLI uses (``Stage1RunService`` with ``outcomes`` wired, exactly as
``enginery.cli.stage1._advancing_service`` now constructs it) actually
produces a durable record the ``enginery outcome`` CLI can read back out
of the same ledger file.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from tests.workflows.test_stage1_runtime import RecordingPullRequests, TerminalWorkLedger, _snapshot

from enginery.application.work_ports import PullRequestPort, WorkLedgerPort
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.ids import (
    NodeId,
    OperationId,
    RunId,
    WorkflowDefinitionId,
)
from enginery.domain.run import Run, RunState
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.review import ReviewReport
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
)


def test_a_dogfooded_stage1_run_produces_an_outcome_observation_the_cli_can_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    database = tmp_path / "ledger.db"
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = replace(
        issue_to_pr_manifest(),
        nodes={
            **issue_to_pr_manifest().nodes,
            NodeId("implement"): replace(
                issue_to_pr_manifest().nodes[NodeId("implement")],
                actor_type=ActorType.DETERMINISTIC,
            ),
        },
    )
    snapshot = _snapshot()
    request = Stage1RunRequest(
        run=Run(
            id=RunId("run-dogfood"),
            work_item_id=snapshot.work_item.id,
            work_item_snapshot_digest=snapshot.work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="repository-1",
            base_revision="base-revision",
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
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=(tmp_path / "workspace").resolve(),
        base_branch="main",
        head_branch="enginery/run-dogfood",
        validation_commands=(("uv", "run", "pytest", "-q"),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:run-dogfood"),
            time_budget_seconds=60,
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/enginery-provider-smoke",
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            harness_provider="omp",
            harness_credential_reference="test-omp-keyring",
            harness_executable="omp",
            artifact_root=(tmp_path / "artifacts").resolve(),
        ),
    )

    ledger = LedgerService.open(database)
    try:
        runtime = CoordinatorRuntime(ledger, owner="coordinator")
        pull_requests = RecordingPullRequests()
        work_ledger = TerminalWorkLedger(snapshot)
        outcomes = OutcomeCaptureService(
            ledger=ledger, pull_requests=cast(PullRequestPort, pull_requests)
        )
        service = Stage1RunService(
            runtime=runtime,
            ledger=ledger,
            work_ledger=cast(WorkLedgerPort, work_ledger),
            pull_requests=cast(PullRequestPort, pull_requests),
            outcomes=outcomes,
        )
        service.start(request, now=now, heartbeat_window=timedelta(seconds=60))

        for node_id in ("qualify", "implement", "validate"):
            dispatch = WorkflowNodeDispatch(
                FixtureDispatch(
                    run_id=str(request.run.id),
                    node_id=node_id,
                    attempt_id=f"{node_id}-0",
                    repository_id=request.repository_id,
                    repository_path=request.repository_path,
                    workspace_path=request.workspace_path,
                    base_revision="base-revision",
                    command=(node_id,),
                    expected_attempt_version=0,
                    operation_id=f"{node_id}:run-dogfood",
                    dependencies=(
                        ()
                        if node_id == "qualify"
                        else ((str(request.run.id), "qualify"),)
                        if node_id == "implement"
                        else ((str(request.run.id), "implement"),)
                    ),
                    workflow_definition_id=manifest.id.value,
                ),
                manifest,
            )
            epoch = runtime.register_node(
                dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60)
            )
            runtime.complete_node(
                run_id=str(request.run.id),
                node_id=node_id,
                epoch=epoch.epoch,
                now=now,
                extra=(
                    {"validation_artifact_digest": str(Digest.of_bytes(b"validation"))}
                    if node_id == "validate"
                    else None
                ),
            )
        ledger.append(
            AppendCommand(
                correlation_id="implementation-artifacts-dogfood",
                events=(
                    EventWrite(
                        aggregate_type="node_attempt",
                        aggregate_id="implement-0",
                        expected_version=0,
                        event_type="node_attempt.result_ingested",
                        schema_version=1,
                        payload={"artifact_references": [str(Digest.of_bytes(b"implementation"))]},
                    ),
                ),
            )
        )
        service.review_implementation(
            request,
            ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
            repair_attempt=0,
            now=now,
            heartbeat_window=timedelta(seconds=60),
        )
        service.open_pull_request(request, now=now, heartbeat_window=timedelta(seconds=60))
        assert (
            service.wait_for_ci(request, now=now, heartbeat_window=timedelta(seconds=60)).value
            == "merge_ready"
        )
        result = service.verify_merge_ready(
            request, now=now, heartbeat_window=timedelta(seconds=60)
        )
        assert result.outcome.value == "merge_ready"

        # This is the exact tick a real `enginery stage1 watch --advance`
        # invocation performs: at most one durable next action.
        service.advance(
            request.run.id,
            now=now,
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=60),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        )
    finally:
        ledger.close()

    # Dogfooding: read the durable result back out through the same
    # `enginery outcome` CLI an operator would use, against the same
    # ledger file the Stage 1 run just wrote to.
    exit_code = main(["outcome", "list", "--database", str(database), "--state", "pending"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = {observation["kind"] for observation in payload["observations"]}
    assert kinds == {"merge_result", "reopened_issue"}
    merge_observation = next(
        observation
        for observation in payload["observations"]
        if observation["kind"] == "merge_result"
    )
    assert merge_observation["run_id"] == "run-dogfood"
    assert merge_observation["detail"]["subject_reference"] == "17"

    show_exit_code = main(["outcome", "show", "--database", str(database), merge_observation["id"]])
    assert show_exit_code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["found"] is True
    assert shown["state"] == "pending"

    completeness_exit_code = main(["outcome", "completeness", "--database", str(database)])
    assert completeness_exit_code == 0
    completeness = json.loads(capsys.readouterr().out)
    assert completeness["pending"] == 2
    assert completeness["completeness"] == 1.0
