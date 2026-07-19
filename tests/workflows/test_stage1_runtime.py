from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import (
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestPort,
    PullRequestRequest,
    PullRequestSnapshot,
    WorkLedgerPort,
    WorkLedgerSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import (
    ExternalConflictError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.domain.ids import NodeId, OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import (
    CoordinatorRuntime,
    FixtureDispatch,
    WorkflowNodeDispatch,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import IssueReadiness, issue_to_pr_manifest
from enginery.workflows.review import ReviewFinding, ReviewOutcome, ReviewReport
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
)
from enginery.workflows.stage1_runtime import (
    Stage1QualificationExecutor,
    Stage1ReviewExecutor,
    Stage1ValidationExecutor,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _request(tmp_path: Path) -> FixtureDispatch:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return FixtureDispatch(
        run_id="run-1",
        node_id="qualify",
        attempt_id="attempt-1",
        repository_id="repository-1",
        repository_path=repository,
        workspace_path=tmp_path / "workspace",
        base_revision=_git("rev-parse", "HEAD", cwd=repository),
        command=("unused",),
        expected_attempt_version=0,
        operation_id="operation-1",
        workflow_definition_id="issue-to-pr-v1",
    )


def _snapshot() -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("work-1"),
            work_kind=WorkKind.ISSUE,
            source_provider="github",
            external_reference="issue:1",
            source_snapshot_reference="issue:1@1",
            title="Bounded change",
            objective="Change one bounded behavior.",
            acceptance_criteria=("observable result",),
            constraints=("retain evidence",),
            risk_class=RiskClass.LOW,
            repository_targets=("repository-1",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="1",
    )


def _review_manifest() -> WorkflowManifest:
    return WorkflowManifest.from_mapping(
        {
            "id": "issue-to-pr-v1",
            "name": "review fixture",
            "schema_version": 1,
            "nodes": {
                "review": {
                    "kind": "request_human_decision",
                    "input_schema": {},
                    "output_schema": {},
                    "actor_type": "human",
                    "side_effect_class": "none",
                    "idempotency_behavior": "not_applicable",
                }
            },
            "terminal_states": ["complete"],
            "terminal_state_mapping": {"review": "complete"},
        }
    )


class RecordingWorkLedger:
    def __init__(
        self,
        ledger: LedgerService,
        snapshot: WorkLedgerSnapshot,
        *,
        expected_run_id: str = "run-1",
        require_registered_run: bool = False,
    ) -> None:
        self.ledger = ledger
        self.snapshot = snapshot
        self.expected_run_id = expected_run_id
        self.require_registered_run = require_registered_run

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        if self.require_registered_run:
            run = self.ledger.read_projection(
                aggregate_type="run", aggregate_id=self.expected_run_id
            )
            assert run is not None
            assert run.state["status"] == "created"
        node = self.ledger.read_projection(
            aggregate_type="runtime_node", aggregate_id=f"{self.expected_run_id}:qualify"
        )
        assert external_reference == "issue:1"
        assert node is not None
        assert node.state["status"] == "queued"
        return self.snapshot


def test_qualification_persists_manifest_node_before_provider_fetch(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    executor = Stage1QualificationExecutor(
        runtime, cast(WorkLedgerPort, RecordingWorkLedger(ledger_service, _snapshot()))
    )

    qualification = executor.qualify(
        dispatch=WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest()),
        external_reference="issue:1",
        applicable_criteria=(True,),
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )

    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:qualify"
    )
    assert qualification.readiness is IssueReadiness.READY
    assert node is not None
    assert node.state["status"] == "passed"
    assert node.state["source_revision"] == "1"


def test_validation_persists_node_before_running_commands(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())

    def run_command(
        command: tuple[str, ...], workspace_path: Path
    ) -> subprocess.CompletedProcess[str]:
        node = ledger_service.read_projection(
            aggregate_type="runtime_node", aggregate_id="run-1:qualify"
        )
        assert command == ("validate",)
        assert workspace_path == dispatch.request.workspace_path
        assert node is not None
        assert node.state["status"] == "queued"
        return subprocess.CompletedProcess(command, 0, "token=0123456789abcdef", "")

    result = Stage1ValidationExecutor(
        runtime=runtime,
        artifact_store=artifact_store,
        command_runner=run_command,
    ).validate(
        dispatch=dispatch,
        commands=(("validate",),),
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )

    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:qualify"
    )
    assert result.passed is True
    assert node is not None
    assert node.state["status"] == "passed"
    assert node.state["validation_artifact_digest"] == str(result.artifact_digest)
    assert b"0123456789abcdef" not in artifact_store.read_bytes(result.artifact_digest)


def test_review_persists_human_wait_and_independent_decision(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    manifest = _review_manifest()
    first_dispatch = WorkflowNodeDispatch(
        replace(
            _request(tmp_path),
            node_id="review",
            attempt_id="review-0",
            operation_id="review-operation-0",
        ),
        manifest,
    )
    report = ReviewReport(
        producer="omp",
        reviewer="human-operator",
        findings=(ReviewFinding("format", actionable=True, blocking=False),),
    )
    first = Stage1ReviewExecutor(runtime).review(
        dispatch=first_dispatch,
        report=report,
        repair_attempt=0,
        repair_limit=1,
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )
    second = Stage1ReviewExecutor(runtime).review(
        dispatch=WorkflowNodeDispatch(
            replace(
                first_dispatch.request,
                attempt_id="review-1",
                operation_id="review-operation-1",
            ),
            manifest,
        ),
        report=report,
        repair_attempt=1,
        repair_limit=1,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
    )

    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:review"
    )
    assert first.outcome is ReviewOutcome.REPAIR_REQUESTED
    assert second.outcome is ReviewOutcome.REPAIR_EXHAUSTED
    assert node is not None
    assert node.state["status"] == "passed"
    assert node.state["review_outcome"] == ReviewOutcome.REPAIR_EXHAUSTED.value


def test_tick_does_not_dispatch_a_recovered_deterministic_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())

    runtime.register_node(dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60))
    tick = runtime.tick(
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
    )

    assert tick.dispatched == ()


def test_manifest_node_dispatch_rejects_agent_nodes(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="non-agent"):
        WorkflowNodeDispatch(
            replace(
                _request(tmp_path),
                node_id="implement",
                dependencies=(("run-1", "qualify"),),
            ),
            issue_to_pr_manifest(),
        )


def test_manifest_registration_renews_its_active_epoch(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())

    first = runtime.register_node(
        dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60)
    )
    second = runtime.register_node(
        dispatch=dispatch,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
    )

    assert second.epoch == first.epoch


def test_manifest_registration_requires_completed_dependencies(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(
        replace(
            _request(tmp_path),
            node_id="validate",
            dependencies=(("run-1", "implement"),),
        ),
        issue_to_pr_manifest(),
    )

    with pytest.raises(ExternalConflictError, match="dependencies"):
        runtime.register_node(dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60))


def test_manifest_node_dispatch_rejects_dependency_bypass(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="dependencies"):
        WorkflowNodeDispatch(
            replace(_request(tmp_path), dependencies=(("run-1", "unrelated"),)),
            issue_to_pr_manifest(),
        )


def test_raw_worker_dispatch_cannot_replace_a_manifest_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    runtime.register_node(dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60))

    with pytest.raises(ExternalConflictError, match="actor type"):
        runtime.tick(
            now=now + timedelta(seconds=1),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            requests=(dispatch.request,),
        )


def test_human_wait_resolution_requires_prior_qualification(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    qualification = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=qualification, now=now, heartbeat_window=timedelta(seconds=60)
    )
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=now)
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

    runtime.register_node(dispatch=approval, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.await_human_node(
        run_id="run-1",
        node_id="plan_approval",
        epoch=epoch.epoch,
        now=now,
        reason="approval required",
    )
    runtime.resolve_human_wait(
        run_id="run-1",
        node_id="plan_approval",
        epoch=epoch.epoch,
        now=now,
        outcome="passed",
        extra={"decision": "approved"},
    )

    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:plan_approval"
    )
    assert projection is not None
    assert projection.state["status"] == "passed"
    assert projection.state["decision"] == "approved"


def test_terminal_node_retry_requires_new_fenced_identity(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    initial = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(dispatch=initial, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=now)
    retry = replace(
        initial.request,
        attempt_id="attempt-2",
        operation_id="operation-2",
    )

    runtime.retry_node(
        request=retry,
        actor_type=ActorType.DETERMINISTIC,
        epoch=epoch.epoch,
        now=now,
    )

    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:qualify"
    )
    assert projection is not None
    assert projection.state["status"] == "queued"
    assert projection.state["attempt_id"] == "attempt-2"


def test_human_wait_resolution_rejects_nonwaiting_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    dispatch = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(
        dispatch=dispatch, now=now, heartbeat_window=timedelta(seconds=60)
    )

    with pytest.raises(ExternalConflictError, match="human-waiting"):
        runtime.resolve_human_wait(
            run_id="run-1",
            node_id="qualify",
            epoch=epoch.epoch,
            now=now,
            outcome="passed",
        )


def test_terminal_node_retry_rejects_reused_or_redefined_request(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    initial = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(dispatch=initial, now=now, heartbeat_window=timedelta(seconds=60))
    runtime.complete_node(run_id="run-1", node_id="qualify", epoch=epoch.epoch, now=now)

    with pytest.raises(InvalidInputError, match="fresh attempt"):
        runtime.retry_node(
            request=replace(initial.request, operation_id="operation-2"),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )
    with pytest.raises(InvalidInputError, match="fresh attempt"):
        runtime.retry_node(
            request=replace(initial.request, attempt_id="attempt-2"),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )
    with pytest.raises(ExternalConflictError, match="actor type"):
        runtime.retry_node(
            request=replace(
                initial.request,
                attempt_id="attempt-2",
                operation_id="operation-2",
            ),
            actor_type=ActorType.HUMAN,
            epoch=epoch.epoch,
            now=now,
        )
    with pytest.raises(ExternalConflictError, match="immutable"):
        runtime.retry_node(
            request=replace(
                initial.request,
                attempt_id="attempt-2",
                operation_id="operation-2",
                base_revision="different-base",
            ),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )


def test_retry_rejects_nonterminal_node(ledger_service: LedgerService, tmp_path: Path) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    initial = WorkflowNodeDispatch(_request(tmp_path), issue_to_pr_manifest())
    epoch = runtime.register_node(dispatch=initial, now=now, heartbeat_window=timedelta(seconds=60))

    with pytest.raises(ExternalConflictError, match="terminal"):
        runtime.retry_node(
            request=replace(
                initial.request,
                attempt_id="attempt-2",
                operation_id="operation-2",
            ),
            actor_type=ActorType.DETERMINISTIC,
            epoch=epoch.epoch,
            now=now,
        )


def test_stage1_run_qualifies_and_launches_omp_only_after_durable_intent(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "enginery@example.test", cwd=repository)
    _git("config", "user.name", "Enginery Test", cwd=repository)
    (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repository)
    _git("commit", "-m", "baseline", cwd=repository)
    base_revision = _git("rev-parse", "HEAD", cwd=repository)
    fake_omp = tmp_path / "fake-omp"
    fake_omp.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "for event in ('session', 'agent_start'):\n"
        "    print(json.dumps({'type': event}))\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_omp.chmod(0o755)
    manifest = issue_to_pr_manifest()
    snapshot = _snapshot()
    request = Stage1RunRequest(
        run=Run(
            id=RunId("run-stage1"),
            work_item_id=snapshot.work_item.id,
            work_item_snapshot_digest=snapshot.work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="repository-1",
            base_revision=base_revision,
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
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/Enginery",
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            omp_credential_reference="test-omp-keyring",
            omp_executable=str(fake_omp),
            artifact_root=(tmp_path / "artifacts").resolve(),
        ),
        workspace_path=tmp_path / "workspace",
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
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
    )
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    service = Stage1RunService(
        runtime=runtime,
        ledger=ledger_service,
        work_ledger=cast(
            WorkLedgerPort,
            RecordingWorkLedger(
                ledger_service,
                snapshot,
                expected_run_id="run-stage1",
                require_registered_run=True,
            ),
        ),
        omp_harness=OmpHarness(
            OmpAdapterConfig(credential_reference="test-keyring", executable=str(fake_omp)),
            ArtifactStore(tmp_path / "artifacts"),
        ),
    )

    first = service.start(
        request,
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )
    assert service.next_action(request.run.id).action.value == "qualify"
    with pytest.raises(MissingPrerequisiteError, match="successful node 'qualify'"):
        service.dispatch_implementation(
            request,
            now=now,
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        )
    progression = service.advance(
        request.run.id,
        now=now + timedelta(seconds=1),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
    )
    assert progression.action.value == "implement"
    assert service.next_action(request.run.id).action.value == "implement"
    implementation = service.dispatch_implementation(
        request,
        now=now + timedelta(seconds=2),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=30),
        limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
    )
    assert service.next_action(request.run.id).action.value == "wait"
    deadline = time.monotonic() + 5
    while not implementation.result_path.is_file():
        if time.monotonic() >= deadline:
            pytest.fail("supervised OMP worker did not retain a result")
        time.sleep(0.01)
    assert service.next_action(request.run.id).action.value == "collect_implementation"
    recovered = Stage1RunService(
        runtime=CoordinatorRuntime(ledger_service, owner="coordinator"),
        ledger=ledger_service,
        omp_harness=OmpHarness(
            OmpAdapterConfig(credential_reference="test-keyring", executable=str(fake_omp)),
            ArtifactStore(tmp_path / "recovered-artifacts"),
        ),
    )
    while True:
        try:
            result = recovered.collect_implementation(request, now=now + timedelta(seconds=3))
        except ExternalConflictError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
        else:
            break
    assert recovered.next_action(request.run.id).action.value == "await_human_review"
    second = service.start(
        request,
        now=now + timedelta(seconds=3),
        heartbeat_window=timedelta(seconds=60),
    )
    conflicting_request = replace(request, head_branch="enginery/other-stage1")
    with pytest.raises(ExternalConflictError, match="different immutable request"):
        service.start(
            conflicting_request,
            now=now + timedelta(seconds=4),
            heartbeat_window=timedelta(seconds=60),
        )
    implementation_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-stage1:implement"
    )
    assert implementation_node is not None
    assert implementation_node.state["status"] == "failed"
    assert implementation_node.state["retain_workspace"] is True
    assert result.terminal_status == "failed"
    epoch = runtime.claim_epoch(
        now=now + timedelta(seconds=5), heartbeat_window=timedelta(seconds=60)
    )
    runtime.release_workspace(
        run_id="run-stage1",
        repository_id="repository-1",
        epoch=epoch.epoch,
        now=now + timedelta(seconds=6),
    )

    assert first.request == request
    assert second.request == request
    assert second.aggregate_version == 1
    qualification_node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-stage1:qualify"
    )
    assert qualification_node is not None
    assert qualification_node.state["status"] == "passed"
    assert qualification_node.state["readiness"] == IssueReadiness.READY.value
    projection = ledger_service.read_projection(aggregate_type="run", aggregate_id="run-stage1")
    assert projection is not None
    assert projection.state["request_digest"] == str(request.digest)
    assert projection.state["status"] == "created"
    assert projection.aggregate_version == 1


@pytest.mark.parametrize(
    ("final_check_status", "final_check_conclusion", "head_revision", "outcome", "next_action"),
    (
        ("completed", "success", "head-revision", "merge_ready", "wait"),
        ("completed", "failure", "head-revision", "blocked", "await_human_review"),
        ("completed", "success", "superseding-head", "superseded", "await_human_review"),
    ),
)
def test_stage1_waits_for_exact_head_ci_before_terminal_progression(
    ledger_service: LedgerService,
    tmp_path: Path,
    final_check_status: str,
    final_check_conclusion: str,
    head_revision: str,
    outcome: str,
    next_action: str,
) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    repository = tmp_path / "repository"
    repository.mkdir()
    template_manifest = issue_to_pr_manifest()
    manifest = replace(
        template_manifest,
        nodes={
            **template_manifest.nodes,
            NodeId("implement"): replace(
                template_manifest.nodes[NodeId("implement")],
                actor_type=ActorType.DETERMINISTIC,
            ),
        },
    )
    snapshot = _snapshot()
    request = Stage1RunRequest(
        run=Run(
            id=RunId("run-pr"),
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
        head_branch="enginery/run-pr",
        validation_commands=(("uv", "run", "pytest", "-q"),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:run-pr"),
            time_budget_seconds=60,
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/enginery-provider-smoke",
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            omp_credential_reference="test-omp-keyring",
            omp_executable="omp",
            artifact_root=(tmp_path / "artifacts").resolve(),
        ),
    )
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    pull_requests = RecordingPullRequests()
    service = Stage1RunService(
        runtime=runtime,
        ledger=ledger_service,
        pull_requests=cast(PullRequestPort, pull_requests),
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
                operation_id=f"{node_id}:run-pr",
                dependencies=(
                    ()
                    if node_id == "qualify"
                    else (
                        (str(request.run.id), "qualify" if node_id == "implement" else "implement"),
                    )
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
        )

    service.review_implementation(
        request,
        ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
        repair_attempt=0,
        now=now,
        heartbeat_window=timedelta(seconds=60),
    )

    pull_requests.failures_remaining = 1
    with pytest.raises(ExternalConflictError, match="simulated provider interruption"):
        service.open_pull_request(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert service.next_action(request.run.id).action.value == "open_pr"
    service.open_pull_request(request, now=now, heartbeat_window=timedelta(seconds=60))

    assert service.next_action(request.run.id).action.value == "wait_for_ci"
    pull_requests.check_status = "in_progress"
    pull_requests.check_conclusion = None
    assert (
        service.wait_for_ci(request, now=now, heartbeat_window=timedelta(seconds=60)).value
        == "waiting"
    )
    assert service.next_action(request.run.id).action.value == "wait_for_ci"
    pull_requests.check_status = final_check_status
    pull_requests.check_conclusion = final_check_conclusion
    pull_requests.head_revision = head_revision
    assert (
        service.wait_for_ci(request, now=now, heartbeat_window=timedelta(seconds=60)).value
        == outcome
    )
    assert service.next_action(request.run.id).action.value == next_action


class RecordingPullRequests:
    """Capture the idempotent Stage 1 pull-request request."""

    def __init__(self) -> None:
        self.requests: list[PullRequestRequest] = []
        self.failures_remaining = 0
        self.check_status = "completed"
        self.check_conclusion: str | None = "success"
        self.head_revision = "head-revision"

    def probe(self) -> object:
        raise AssertionError("not used by this test")

    def create_or_update(self, request: PullRequestRequest) -> PullRequestSnapshot:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ExternalConflictError("simulated provider interruption")
        self.requests.append(request)
        return PullRequestSnapshot(
            number=17,
            url="https://example.test/pull/17",
            state="open",
            head_branch=request.head_branch,
            head_revision="head-revision",
            base_branch=request.base_branch,
            base_revision="base-revision",
        )

    def get(self, number: int) -> PullRequestSnapshot:
        if number != 17 or not self.requests:
            raise AssertionError("unexpected pull request lookup")
        request = self.requests[-1]
        return PullRequestSnapshot(
            number=17,
            url="https://example.test/pull/17",
            state="open",
            head_branch=request.head_branch,
            head_revision=self.head_revision,
            base_branch=request.base_branch,
            base_revision="base-revision",
        )

    def evidence(self, number: int) -> PullRequestEvidence:
        if number != 17:
            raise AssertionError("unexpected pull request number")
        snapshot = self.get(number)
        return PullRequestEvidence(
            pull_request=snapshot,
            reviews=(),
            checks=(
                PullRequestCheck(
                    name="CI",
                    status=self.check_status,
                    conclusion=self.check_conclusion,
                    head_revision=snapshot.head_revision,
                ),
            ),
            mergeable=True,
        )

    def reconcile(self, *, operation_id: OperationId) -> object:
        raise AssertionError("not used by this test")
