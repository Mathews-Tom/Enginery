"""Durable Stage 1 run intent composed through the coordinator runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from enginery.adapters.omp import OmpHarness
from enginery.application.work_ports import (
    HarnessResult,
    HarnessTask,
    PullRequestPort,
    PullRequestRequest,
    WorkLedgerPort,
    WorkLedgerSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.run import Run, RunState
from enginery.domain.serialization import (
    run_from_dict,
    run_to_dict,
    work_item_from_dict,
    work_item_to_dict,
    workflow_manifest_from_dict,
    workflow_manifest_to_dict,
)
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.runtime import (
    RUN_AGGREGATE_TYPE,
    RUNTIME_NODE_AGGREGATE_TYPE,
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
    WorkflowDispatch,
    WorkflowNodeDispatch,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.implementation import Stage1ImplementationExecutor
from enginery.workflows.issue_to_pr import IssueQualification
from enginery.workflows.review import ReviewOutcome, ReviewReport
from enginery.workflows.stage1_runtime import (
    Stage1QualificationExecutor,
    Stage1ReviewExecutor,
    Stage1ReviewResult,
    Stage1ValidationExecutor,
    Stage1ValidationResult,
)


@dataclass(frozen=True, slots=True)
class Stage1RunRequest:
    """Immutable configuration bound before a Stage 1 run can progress."""

    run: Run
    work_snapshot: WorkLedgerSnapshot
    manifest: WorkflowManifest
    repository_id: str
    repository_path: Path
    workspace_path: Path
    base_branch: str
    head_branch: str
    validation_commands: tuple[tuple[str, ...], ...]
    applicable_criteria: tuple[bool, ...]
    required_checks: tuple[str, ...]
    repair_limit: int
    implementation: Stage1ImplementationRequest
    execution_configuration: Stage1ExecutionConfiguration

    def __post_init__(self) -> None:
        if self.run.state is not RunState.CREATED:
            raise InvalidInputError("a Stage 1 run must start in the created state")
        if self.run.workflow_definition_id != self.manifest.id:
            raise InvalidInputError("Stage 1 run must bind its manifest identity")
        if self.run.workflow_definition_digest != self.manifest.content_digest:
            raise InvalidInputError("Stage 1 run must bind its manifest digest")
        if self.run.work_item_id != self.work_snapshot.work_item.id:
            raise InvalidInputError("Stage 1 run must bind its work-item identity")
        if self.run.work_item_snapshot_digest != self.work_snapshot.work_item.bound_field_digest:
            raise InvalidInputError("Stage 1 run must bind its work-item digest")
        if self.repository_id not in self.work_snapshot.work_item.repository_targets:
            raise InvalidInputError("Stage 1 run repository is not an approved work-item target")
        if not self.repository_path.is_absolute() or not self.workspace_path.is_absolute():
            raise InvalidInputError("Stage 1 repository and workspace paths must be absolute")
        if not self.base_branch.strip() or not self.head_branch.strip():
            raise InvalidInputError("Stage 1 branch names must be non-blank")
        if self.base_branch == self.head_branch:
            raise InvalidInputError("Stage 1 head and base branches must differ")
        if not self.validation_commands:
            raise InvalidInputError("Stage 1 requires at least one validation command")
        invalid_validation_command = any(
            not command or any(not argument.strip() for argument in command)
            for command in self.validation_commands
        )
        if invalid_validation_command:
            raise InvalidInputError("Stage 1 validation commands must contain non-blank arguments")
        if len(self.applicable_criteria) != len(self.work_snapshot.work_item.acceptance_criteria):
            raise InvalidInputError(
                "Stage 1 applicability must classify every acceptance criterion"
            )
        if not self.required_checks or any(not check.strip() for check in self.required_checks):
            raise InvalidInputError("Stage 1 requires non-blank exact-head checks")
        if self.repair_limit < 0:
            raise InvalidInputError("Stage 1 repair_limit cannot be negative")

    @property
    def digest(self) -> Digest:
        """Return the immutable request digest used for idempotent start."""
        return Digest.of_json(self._state())

    def initial_state(self) -> dict[str, object]:
        """Return the complete initial run projection stored before progression."""
        state = self._state()
        state["status"] = self.run.state.value
        state["request_digest"] = str(self.digest)
        return state

    def _state(self) -> dict[str, object]:
        execution = self.execution_configuration
        return {
            "run_id": str(self.run.id),
            "run": run_to_dict(self.run),
            "work_item": work_item_to_dict(self.work_snapshot.work_item),
            "source_revision": self.work_snapshot.source_revision,
            "manifest": workflow_manifest_to_dict(self.manifest),
            "repository_id": self.repository_id,
            "repository_path": str(self.repository_path),
            "workspace_path": str(self.workspace_path),
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "validation_commands": [list(command) for command in self.validation_commands],
            "applicable_criteria": list(self.applicable_criteria),
            "required_checks": list(self.required_checks),
            "repair_limit": self.repair_limit,
            "implementation": {
                "attempt_id": self.implementation.attempt_id,
                "operation_id": str(self.implementation.operation_id),
                "time_budget_seconds": self.implementation.time_budget_seconds,
                "cost_budget": (
                    None
                    if self.implementation.cost_budget is None
                    else str(self.implementation.cost_budget)
                ),
                "permitted_capabilities": list(self.implementation.permitted_capabilities),
                "evidence_requirements": list(self.implementation.evidence_requirements),
            },
            "execution_configuration": {
                "github_repository": execution.github_repository,
                "github_credential_reference": execution.github_credential_reference,
                "github_executable": execution.github_executable,
                "omp_credential_reference": execution.omp_credential_reference,
                "omp_executable": execution.omp_executable,
                "artifact_root": str(execution.artifact_root),
            },
        }


@dataclass(frozen=True, slots=True)
class Stage1ExecutionConfiguration:
    """Opaque provider configuration persisted before any Stage 1 effect."""

    github_repository: str
    github_credential_reference: str
    github_executable: str
    omp_credential_reference: str
    omp_executable: str
    artifact_root: Path

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.github_repository,
                self.github_credential_reference,
                self.github_executable,
                self.omp_credential_reference,
                self.omp_executable,
            )
        ):
            raise InvalidInputError("Stage 1 provider configuration must be non-blank")
        if not self.artifact_root.is_absolute():
            raise InvalidInputError("Stage 1 artifact root must be absolute")


@dataclass(frozen=True, slots=True)
class Stage1Run:
    """A durable Stage 1 run projection decoded from the ledger."""

    request: Stage1RunRequest
    status: RunState
    aggregate_version: int


@dataclass(frozen=True, slots=True)
class Stage1ImplementationRequest:
    """Bound OMP attempt configuration for the manifest's implementation node."""

    attempt_id: str
    operation_id: OperationId
    time_budget_seconds: int
    cost_budget: Decimal | None
    permitted_capabilities: tuple[str, ...]
    evidence_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.attempt_id.strip() or not str(self.operation_id).strip():
            raise InvalidInputError("Stage 1 implementation IDs must be non-blank")
        if self.time_budget_seconds < 1:
            raise InvalidInputError("Stage 1 implementation time budget must be positive")
        if self.cost_budget is not None and (
            not self.cost_budget.is_finite() or self.cost_budget < 0
        ):
            raise InvalidInputError(
                "Stage 1 implementation cost budget must be finite and non-negative"
            )
        if any(not value.strip() for value in self.permitted_capabilities):
            raise InvalidInputError("Stage 1 permitted capabilities must be non-blank")
        if any(not value.strip() for value in self.evidence_requirements):
            raise InvalidInputError("Stage 1 evidence requirements must be non-blank")


class Stage1ProgressionAction(StrEnum):
    """One safe next action derived from durable Stage 1 projections."""

    QUALIFY = "qualify"
    IMPLEMENT = "implement"
    COLLECT_IMPLEMENTATION = "collect_implementation"
    VALIDATE = "validate"
    AWAIT_HUMAN_REVIEW = "await_human_review"
    OPEN_PR = "open_pr"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class Stage1Progression:
    """The next Stage 1 action and the durable run it was derived from."""

    action: Stage1ProgressionAction
    run: Stage1Run


@dataclass(frozen=True, slots=True)
class Stage1ImplementationDispatch:
    """One supervised OMP attempt launched by the coordinator runtime."""

    task: HarnessTask
    fixture: DispatchedFixture
    result_path: Path


@dataclass(frozen=True, slots=True)
class Stage1RunService:
    """Create and read Stage 1 runs through the sole coordinator runtime."""

    runtime: CoordinatorRuntime
    ledger: LedgerService
    work_ledger: WorkLedgerPort | None = None
    pull_requests: PullRequestPort | None = None
    omp_harness: OmpHarness | None = None

    def start(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1Run:
        """Persist an immutable run intent before any workflow side effect."""
        self.runtime.register_run(
            run_id=str(request.run.id),
            initial_state=request.initial_state(),
            now=now,
            heartbeat_window=heartbeat_window,
        )
        return self.read(request.run.id)

    def read(self, run_id: RunId) -> Stage1Run:
        """Read one complete durable run projection or fail loudly."""
        projection = self.ledger.read_projection(
            aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=str(run_id)
        )
        if projection is None:
            raise InternalInvariantViolationError(
                "Stage 1 run projection is missing", details={"run_id": str(run_id)}
            )
        return _run_from_state(projection.state, aggregate_version=projection.aggregate_version)

    def next_action(self, run_id: RunId) -> Stage1Progression:
        """Derive at most one safe action from the durable run and node projections."""
        run = self.read(run_id)
        qualification_status = self._node_status(run_id, "qualify")
        if qualification_status is None:
            return Stage1Progression(Stage1ProgressionAction.QUALIFY, run)
        if qualification_status != "passed":
            action = (
                Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
                if qualification_status in {"failed", "cancelled", "blocked"}
                else Stage1ProgressionAction.WAIT
            )
            return Stage1Progression(action, run)

        implementation_status = self._node_status(run_id, "implement")
        if implementation_status is None:
            return Stage1Progression(Stage1ProgressionAction.IMPLEMENT, run)
        if implementation_status in {"failed", "cancelled", "blocked"}:
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        if implementation_status != "passed":
            result_path = _implementation_result_path(run.request)
            action = (
                Stage1ProgressionAction.COLLECT_IMPLEMENTATION
                if result_path.is_file()
                else Stage1ProgressionAction.WAIT
            )
            return Stage1Progression(action, run)

        validation_status = self._node_status(run_id, "validate")
        if validation_status is None:
            return Stage1Progression(Stage1ProgressionAction.VALIDATE, run)
        if validation_status != "passed":
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)

        review_status = self._node_status(run_id, "review")
        if review_status != "passed":
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        if self._review_outcome(run_id) is not ReviewOutcome.APPROVED:
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        open_pr_status = self._node_status(run_id, "open_pr")
        if open_pr_status in {None, "queued"}:
            return Stage1Progression(Stage1ProgressionAction.OPEN_PR, run)
        if open_pr_status != "passed":
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        return Stage1Progression(Stage1ProgressionAction.WAIT, run)

    def advance(
        self,
        run_id: RunId,
        *,
        now: datetime,
        heartbeat_window: timedelta,
        lease_window: timedelta,
        limits: SchedulingLimits,
    ) -> Stage1Progression:
        """Perform at most one externally observable action selected from durable state."""
        progression = self.next_action(run_id)
        request = progression.run.request
        if progression.action is Stage1ProgressionAction.QUALIFY:
            self.qualify(
                request,
                external_reference=request.work_snapshot.work_item.external_reference,
                applicable_criteria=request.applicable_criteria,
                now=now,
                heartbeat_window=heartbeat_window,
            )
        elif progression.action is Stage1ProgressionAction.IMPLEMENT:
            self.dispatch_implementation(
                request,
                now=now,
                heartbeat_window=heartbeat_window,
                lease_window=lease_window,
                limits=limits,
            )
        elif progression.action is Stage1ProgressionAction.COLLECT_IMPLEMENTATION:
            self.collect_implementation(request, now=now)
        elif progression.action is Stage1ProgressionAction.VALIDATE:
            self.validate_implementation(request, now=now, heartbeat_window=heartbeat_window)
        elif progression.action is Stage1ProgressionAction.OPEN_PR:
            self.open_pull_request(request, now=now, heartbeat_window=heartbeat_window)
        return self.next_action(run_id)

    def qualify(
        self,
        request: Stage1RunRequest,
        *,
        external_reference: str,
        applicable_criteria: tuple[bool, ...],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> IssueQualification:
        """Persist intent, then source-bind and classify the issue through the runtime."""
        self.start(request, now=now, heartbeat_window=heartbeat_window)
        dispatch = WorkflowNodeDispatch(
            _fixture_dispatch(
                request,
                node_id="qualify",
                attempt_id="qualification-0",
                operation_id=f"qualify:{request.run.id}",
                command=request.validation_commands[0],
            ),
            request.manifest,
        )
        return Stage1QualificationExecutor(
            runtime=self.runtime, work_ledger=self._require_work_ledger()
        ).qualify(
            dispatch=dispatch,
            external_reference=external_reference,
            applicable_criteria=applicable_criteria,
            now=now,
            heartbeat_window=heartbeat_window,
        )

    def dispatch_implementation(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
        lease_window: timedelta,
        limits: SchedulingLimits,
    ) -> Stage1ImplementationDispatch:
        """Launch one OMP attempt only after durable successful qualification."""
        self._require_passed_node(request.run.id, "qualify")
        execution = request.implementation
        task = _implementation_task(request)
        result_path = _implementation_result_path(request)
        dispatch = WorkflowDispatch(
            _fixture_dispatch(
                request,
                node_id="implement",
                attempt_id=execution.attempt_id,
                operation_id=str(execution.operation_id),
                command=self._require_omp_harness().supervised_command(
                    task, result_path=result_path
                ),
                dependencies=((str(request.run.id), "qualify"),),
            ),
            request.manifest,
        )
        tick = self.runtime.tick(
            now=now,
            heartbeat_window=heartbeat_window,
            lease_window=lease_window,
            limits=limits,
            requests=(dispatch,),
        )
        matching = tuple(
            fixture
            for fixture in tick.dispatched
            if fixture.lease.run_id == str(request.run.id)
            and fixture.lease.node_id == "implement"
            and fixture.lease.attempt_id == execution.attempt_id
            and fixture.lease.operation_id == str(execution.operation_id)
        )
        if len(matching) != 1:
            raise ExternalConflictError(
                "qualified implementation was not scheduled",
                details={
                    "run_id": str(request.run.id),
                    "dispatched_nodes": [
                        f"{fixture.lease.run_id}:{fixture.lease.node_id}"
                        for fixture in tick.dispatched
                    ],
                },
            )
        fixture = matching[0]
        return Stage1ImplementationDispatch(task=task, fixture=fixture, result_path=result_path)

    def collect_implementation(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
    ) -> HarnessResult:
        """Ingest a completed supervised OMP result from durable runtime state."""
        return Stage1ImplementationExecutor(
            runtime=self.runtime,
            harness=self._require_omp_harness(),
            manifest=request.manifest,
        ).collect(
            dispatched=self.runtime.recover_dispatched(
                run_id=str(request.run.id),
                node_id="implement",
            ),
            task=_implementation_task(request),
            now=now,
            result_path=_implementation_result_path(request),
        )

    def validate_implementation(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1ValidationResult:
        """Run configured focused validation after the OMP result is durable."""
        self._require_passed_node(request.run.id, "implement")
        return Stage1ValidationExecutor(
            runtime=self.runtime,
            artifact_store=self._require_omp_harness().artifact_store,
        ).validate(
            dispatch=WorkflowNodeDispatch(
                _fixture_dispatch(
                    request,
                    node_id="validate",
                    attempt_id="validate-0",
                    operation_id=str(
                        OperationId.derive(
                            run_id=request.run.id,
                            node_id=NodeId("validate"),
                            side_effect_kind="validation",
                            target_scope=request.repository_id,
                            ordinal=0,
                        )
                    ),
                    command=request.validation_commands[0],
                    dependencies=((str(request.run.id), "implement"),),
                ),
                request.manifest,
            ),
            commands=request.validation_commands,
            now=now,
            heartbeat_window=heartbeat_window,
        )

    def review_implementation(
        self,
        request: Stage1RunRequest,
        report: ReviewReport,
        *,
        repair_attempt: int,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1ReviewResult:
        """Record an independent review only after passed validation."""
        self._require_passed_node(request.run.id, "validate")
        return Stage1ReviewExecutor(self.runtime).review(
            dispatch=WorkflowNodeDispatch(
                _fixture_dispatch(
                    request,
                    node_id="review",
                    attempt_id=f"review-{repair_attempt}",
                    operation_id=str(
                        OperationId.derive(
                            run_id=request.run.id,
                            node_id=NodeId("review"),
                            side_effect_kind="review",
                            target_scope=request.repository_id,
                            ordinal=repair_attempt,
                        )
                    ),
                    command=("review",),
                    dependencies=((str(request.run.id), "validate"),),
                ),
                request.manifest,
            ),
            report=report,
            repair_attempt=repair_attempt,
            repair_limit=request.repair_limit,
            now=now,
            heartbeat_window=heartbeat_window,
        )

    def open_pull_request(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> None:
        """Persist a PR intent, then create or reconcile exactly one source-host PR."""
        self._require_passed_node(request.run.id, "review")
        if self._review_outcome(request.run.id) is not ReviewOutcome.APPROVED:
            raise MissingPrerequisiteError("Stage 1 requires an approved independent review")
        operation_id = OperationId.derive(
            run_id=request.run.id,
            node_id=NodeId("open_pr"),
            side_effect_kind="pull_request",
            target_scope=request.repository_id,
            ordinal=0,
        )
        dispatch = WorkflowNodeDispatch(
            _fixture_dispatch(
                request,
                node_id="open_pr",
                attempt_id="open-pr-0",
                operation_id=str(operation_id),
                command=("open_pr",),
                dependencies=((str(request.run.id), "review"),),
            ),
            request.manifest,
        )
        epoch = self.runtime.register_node(
            dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
        )
        pull_request = self._require_pull_requests().create_or_update(
            PullRequestRequest(
                head_branch=request.head_branch,
                base_branch=request.base_branch,
                title=request.work_snapshot.work_item.objective,
                body=(
                    f"Implements {request.work_snapshot.work_item.external_reference}.\n\n"
                    f"Enginery run: {request.run.id}"
                ),
                operation_id=operation_id,
            )
        )
        self.runtime.complete_node(
            run_id=str(request.run.id),
            node_id="open_pr",
            epoch=epoch.epoch,
            now=now,
            outcome="passed",
            extra={
                "pull_request_number": pull_request.number,
                "head_revision": pull_request.head_revision,
                "base_revision": pull_request.base_revision,
            },
        )

    def _node_status(self, run_id: RunId, node_id: str) -> str | None:
        projection = self.ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
            aggregate_id=f"{run_id}:{node_id}",
        )
        if projection is None:
            return None
        status = projection.state.get("status")
        if not isinstance(status, str):
            raise InternalInvariantViolationError(
                "Stage 1 runtime node projection has an invalid status",
                details={"run_id": str(run_id), "node_id": node_id},
            )
        return status

    def _review_outcome(self, run_id: RunId) -> ReviewOutcome | None:
        projection = self.ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
            aggregate_id=f"{run_id}:review",
        )
        if projection is None:
            return None
        value = projection.state.get("review_outcome")
        if value is None:
            return None
        if not isinstance(value, str):
            raise InternalInvariantViolationError(
                "Stage 1 review outcome has an invalid type",
                details={"run_id": str(run_id)},
            )
        try:
            return ReviewOutcome(value)
        except ValueError as error:
            raise InternalInvariantViolationError(
                "Stage 1 review outcome is invalid",
                details={"run_id": str(run_id), "review_outcome": value},
            ) from error

    def _require_passed_node(self, run_id: RunId, node_id: str) -> None:
        projection = self.ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
            aggregate_id=f"{run_id}:{node_id}",
        )
        if projection is None or projection.state.get("status") != "passed":
            raise MissingPrerequisiteError(
                f"Stage 1 requires successful node {node_id!r}",
                details={"run_id": str(run_id), "node_id": node_id},
            )

    def _require_work_ledger(self) -> WorkLedgerPort:
        if self.work_ledger is None:
            raise MissingPrerequisiteError("Stage 1 source-work ledger is not configured")
        return self.work_ledger

    def _require_pull_requests(self) -> PullRequestPort:
        if self.pull_requests is None:
            raise MissingPrerequisiteError("Stage 1 pull-request provider is not configured")
        return self.pull_requests

    def _require_omp_harness(self) -> OmpHarness:
        if self.omp_harness is None:
            raise MissingPrerequisiteError("Stage 1 OMP harness is not configured")
        return self.omp_harness


def _implementation_task(request: Stage1RunRequest) -> HarnessTask:
    execution = request.implementation
    return HarnessTask(
        run_id=request.run.id,
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId(execution.attempt_id),
        operation_id=execution.operation_id,
        workspace_path=request.workspace_path,
        objective=request.work_snapshot.work_item.objective,
        acceptance_criteria=request.work_snapshot.work_item.acceptance_criteria,
        constraints=request.work_snapshot.work_item.constraints,
        permitted_capabilities=execution.permitted_capabilities,
        evidence_requirements=execution.evidence_requirements,
        time_budget_seconds=execution.time_budget_seconds,
        cost_budget=execution.cost_budget,
    )


def _implementation_result_path(request: Stage1RunRequest) -> Path:
    return request.workspace_path / (
        f".enginery-omp-{Digest.of_bytes(str(request.implementation.operation_id).encode())}.json"
    )


def _fixture_dispatch(
    request: Stage1RunRequest,
    *,
    node_id: str,
    attempt_id: str,
    operation_id: str,
    command: tuple[str, ...],
    dependencies: tuple[tuple[str, str], ...] = (),
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id=str(request.run.id),
        node_id=node_id,
        attempt_id=attempt_id,
        repository_id=request.repository_id,
        repository_path=request.repository_path,
        workspace_path=request.workspace_path,
        base_revision=request.run.base_revision,
        command=command,
        expected_attempt_version=0,
        operation_id=operation_id,
        dependencies=dependencies,
        workflow_definition_id=request.manifest.id.value,
        retain_workspace=node_id == "implement",
    )


def stage1_request_from_state(state: object) -> Stage1RunRequest:
    """Decode a complete persisted-start request from its JSON-compatible state."""
    return _run_from_state(state, aggregate_version=0).request


def _run_from_state(state: object, *, aggregate_version: int) -> Stage1Run:
    if not isinstance(state, dict):
        raise InvalidInputError("Stage 1 run projection must be a mapping")
    run = run_from_dict(_mapping(state, "run"))
    work_item = work_item_from_dict(_mapping(state, "work_item"))
    source_revision = _string(state, "source_revision")
    manifest = workflow_manifest_from_dict(_mapping(state, "manifest"))
    commands_value = state.get("validation_commands")
    if not isinstance(commands_value, list) or not all(
        isinstance(command, list) for command in commands_value
    ):
        raise InvalidInputError("Stage 1 validation_commands must be a list of lists")
    applicable_criteria_value = state.get("applicable_criteria")
    if not isinstance(applicable_criteria_value, list) or not all(
        isinstance(value, bool) for value in applicable_criteria_value
    ):
        raise InvalidInputError("Stage 1 applicable_criteria must be a list of booleans")
    required_checks_value = state.get("required_checks")
    if not isinstance(required_checks_value, list) or not all(
        isinstance(check, str) for check in required_checks_value
    ):
        raise InvalidInputError("Stage 1 required_checks must be a list of strings")
    implementation = _implementation_from_state(_mapping(state, "implementation"))
    execution_configuration = _execution_configuration_from_state(
        _mapping(state, "execution_configuration")
    )
    status = RunState(_string(state, "status"))
    request = Stage1RunRequest(
        run=run,
        work_snapshot=WorkLedgerSnapshot(work_item=work_item, source_revision=source_revision),
        manifest=manifest,
        repository_id=_string(state, "repository_id"),
        repository_path=Path(_string(state, "repository_path")),
        workspace_path=Path(_string(state, "workspace_path")),
        base_branch=_string(state, "base_branch"),
        head_branch=_string(state, "head_branch"),
        validation_commands=tuple(
            tuple(_string_from_list(argument, "validation_commands") for argument in command)
            for command in commands_value
        ),
        applicable_criteria=tuple(applicable_criteria_value),
        execution_configuration=execution_configuration,
        required_checks=tuple(required_checks_value),
        repair_limit=_integer(state, "repair_limit"),
        implementation=implementation,
    )
    if str(run.id) != _string(state, "run_id"):
        raise InvalidInputError("Stage 1 run projection has a mismatched run_id")
    if status is not run.state:
        raise InvalidInputError("Stage 1 run projection status must match its run state")
    if state.get("request_digest") != str(request.digest):
        raise InvalidInputError("Stage 1 run projection has a mismatched request digest")
    return Stage1Run(request=request, status=status, aggregate_version=aggregate_version)


def _mapping(state: dict[str, object], field_name: str) -> dict[str, object]:
    value = state.get(field_name)
    if not isinstance(value, dict):
        raise InvalidInputError(f"Stage 1 run projection {field_name} must be a mapping")
    return value


def _string(state: dict[str, object], field_name: str) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"Stage 1 run projection {field_name} must be a non-blank string")
    return value


def _string_from_list(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"Stage 1 run projection {field_name} must contain non-blank strings"
        )
    return value


def _implementation_from_state(state: dict[str, object]) -> Stage1ImplementationRequest:
    cost_budget_raw = state.get("cost_budget")
    if cost_budget_raw is None:
        cost_budget = None
    elif isinstance(cost_budget_raw, str):
        try:
            cost_budget = Decimal(cost_budget_raw)
        except (InvalidOperation, ValueError) as error:
            raise InvalidInputError(
                "Stage 1 implementation cost_budget must be a decimal"
            ) from error
    else:
        raise InvalidInputError("Stage 1 implementation cost_budget must be a string or null")
    capabilities = state.get("permitted_capabilities")
    evidence_requirements = state.get("evidence_requirements")
    if not isinstance(capabilities, list) or not isinstance(evidence_requirements, list):
        raise InvalidInputError("Stage 1 implementation capabilities and evidence must be lists")
    return Stage1ImplementationRequest(
        attempt_id=_string(state, "attempt_id"),
        operation_id=OperationId(_string(state, "operation_id")),
        time_budget_seconds=_integer(state, "time_budget_seconds"),
        cost_budget=cost_budget,
        permitted_capabilities=tuple(
            _string_from_list(value, "permitted_capabilities") for value in capabilities
        ),
        evidence_requirements=tuple(
            _string_from_list(value, "evidence_requirements") for value in evidence_requirements
        ),
    )


def _execution_configuration_from_state(
    state: dict[str, object],
) -> Stage1ExecutionConfiguration:
    return Stage1ExecutionConfiguration(
        github_repository=_string(state, "github_repository"),
        github_credential_reference=_string(state, "github_credential_reference"),
        github_executable=_string(state, "github_executable"),
        omp_credential_reference=_string(state, "omp_credential_reference"),
        omp_executable=_string(state, "omp_executable"),
        artifact_root=Path(_string(state, "artifact_root")),
    )


def _integer(state: dict[str, object], field_name: str) -> int:
    value = state.get(field_name)
    if not isinstance(value, int):
        raise InvalidInputError(f"Stage 1 run projection {field_name} must be an integer")
    return value


__all__ = [
    "Stage1ImplementationDispatch",
    "Stage1ImplementationRequest",
    "Stage1Run",
    "Stage1RunRequest",
    "Stage1RunService",
    "stage1_request_from_state",
]
