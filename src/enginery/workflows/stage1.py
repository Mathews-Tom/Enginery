"""Durable Stage 1 run intent composed through the coordinator runtime."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from enginery.application.behavioral_checks import BehavioralCheck, checks_from_state
from enginery.application.branch_publication import (
    BranchPublicationPort,
    BranchPublicationRequest,
    ProtectedPathPolicy,
    PublishedBranch,
)
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.runner_ports import (
    IsolatedCommandRunner,
    RunnerAdmissionPort,
)
from enginery.application.work_ports import (
    DeclaredWorkClassification,
    HarnessResult,
    HarnessTask,
    PullRequestEvidence,
    PullRequestPort,
    PullRequestRequest,
    WorkLedgerPort,
    WorkLedgerSnapshot,
)
from enginery.application.worker_environment import LAUNCHER_KEYS, TRUSTED_LOCAL_WORKER_KEYS
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    ExternalConflictError,
    HumanActionRequiredError,
    InternalInvariantViolationError,
    InvalidInputError,
    MissingPrerequisiteError,
    OperationTimeoutError,
    StaleEvidenceError,
)
from enginery.domain.execution_catalog import ExecutionCatalog, InstalledHost
from enginery.domain.execution_selection import (
    ExecutionHold,
    ExecutionSelection,
    HoldReason,
    detect_catalog_drift,
    detect_host_drift,
    validate_against_catalog,
)
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.outcome import OutcomeKind
from enginery.domain.run import Run, RunState
from enginery.domain.runner import LEGACY_RUNNER_SELECTION, RunnerSelection
from enginery.domain.serialization import (
    run_from_dict,
    run_to_dict,
    work_item_from_dict,
    work_item_to_dict,
    workflow_manifest_from_dict,
    workflow_manifest_to_dict,
)
from enginery.domain.workflow.manifest import CHECK_BINDING_SCHEMA_VERSION, WorkflowManifest
from enginery.domain.workflow.shell_guard import reject_shell_invocation
from enginery.engine.recovery import GONE, WorkerLiveness, assess_worker
from enginery.engine.runtime import (
    BRANCH_PUBLICATION_OPERATOR,
    BRANCH_PUBLICATION_WORKER,
    RUN_AGGREGATE_TYPE,
    RUNTIME_NODE_AGGREGATE_TYPE,
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
    WorkflowDispatch,
    WorkflowNodeDispatch,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.task_repository import out_path_for, resolve_commit, verified_head_checkout
from enginery.engine.workspace import WORKSPACE_MODE_GIT_WORKTREE, WORKSPACE_MODE_TASK_LOCAL_CLONE
from enginery.evaluation.outcomes import OutcomeCaptureService, observation_id_for
from enginery.evidence.acceptance_baseline import load_acceptance_baseline
from enginery.ledger.service import LedgerService
from enginery.workflows.implementation import Stage1ImplementationExecutor, SupervisedHarness
from enginery.workflows.issue_to_pr import WorkQualification
from enginery.workflows.pull_request import (
    PullRequestOutcome,
    PullRequestRequirements,
    evaluate_pull_request,
)
from enginery.workflows.review import ReviewOutcome, ReviewReport
from enginery.workflows.stage1_runtime import (
    MissingToolProbingRunner,
    Stage1QualificationExecutor,
    Stage1ReviewExecutor,
    Stage1ReviewResult,
    Stage1ValidationExecutor,
    Stage1ValidationResult,
    run_sanitized_validation_command,
)
from enginery.workflows.verification import (
    Stage1VerificationExecutor,
    Stage1VerificationRequest,
    Stage1VerificationResult,
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
    execution_selection: ExecutionSelection
    runner: RunnerSelection = LEGACY_RUNNER_SELECTION
    protected_paths: tuple[str, ...] = ()
    behavioral_checks: tuple[BehavioralCheck, ...] | None = None

    def __post_init__(self) -> None:
        if self.run.state is not RunState.CREATED:
            raise InvalidInputError("a Stage 1 run must start in the created state")
        if self.run.workflow_definition_id != self.manifest.id:
            raise InvalidInputError("Stage 1 run must bind its manifest identity")
        if self.run.workflow_definition_digest != self.manifest.content_digest:
            raise InvalidInputError("Stage 1 run must bind its manifest digest")
        if self.run.work_item_id != self.work_snapshot.work_item.id:
            raise InvalidInputError("Stage 1 run must bind its work-item identity")
        if self.run.work_item_snapshot_digest != self.work_snapshot.bound_digest:
            raise InvalidInputError("Stage 1 run must bind its source snapshot digest")
        if self.run.repository != self.repository_id:
            raise InvalidInputError("Stage 1 run repository must match the source-bound target")
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
        ProtectedPathPolicy(self.protected_paths)
        if self.execution_selection.harness != self.execution_configuration.harness_provider:
            raise InvalidInputError(
                "Stage 1 execution selection harness must match the configured harness provider",
                details={
                    "selection_harness": self.execution_selection.harness,
                    "harness_provider": self.execution_configuration.harness_provider,
                },
            )

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
        state: dict[str, object] = {
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
                "harness_provider": execution.harness_provider,
                "harness_credential_reference": execution.harness_credential_reference,
                "harness_executable": execution.harness_executable,
                "artifact_root": str(execution.artifact_root),
            },
        }
        if self.work_snapshot.classification_provenance is not None:
            state["classification_provenance"] = (
                self.work_snapshot.classification_provenance.to_state()
            )
        if not self.execution_selection.legacy:
            # A run persisted before execution selections existed has no selection key, so
            # its state, and therefore its request digest, is exactly what it was.
            state["execution_selection"] = self.execution_selection.to_state()
        if not self.runner.legacy:
            # A run persisted before trust modes existed has no runner key, so its state, and
            # therefore its request digest, is exactly what it was.
            state["runner"] = self.runner.to_state()
        if self.protected_paths:
            # Extra protected-path patterns only widen the default set. A request without any has
            # no key, so every earlier run keeps the state and request digest it was persisted
            # with.
            state["protected_paths"] = list(self.protected_paths)
        if self.behavioral_checks is not None:
            state["behavioral_checks"] = [check.to_state() for check in self.behavioral_checks]
        return state


@dataclass(frozen=True, slots=True)
class Stage1ExecutionConfiguration:
    """Opaque provider configuration persisted before any Stage 1 effect."""

    github_repository: str
    github_credential_reference: str
    github_executable: str
    harness_provider: str
    harness_credential_reference: str
    harness_executable: str
    artifact_root: Path

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.github_repository,
                self.github_credential_reference,
                self.github_executable,
                self.harness_provider,
                self.harness_credential_reference,
                self.harness_executable,
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
    DISPATCH_QUEUED = "dispatch_queued"
    RECONCILE_WORKER = "reconcile_worker"
    COLLECT_IMPLEMENTATION = "collect_implementation"
    VALIDATE = "validate"
    AWAIT_HUMAN_REVIEW = "await_human_review"
    BLOCKED = "blocked"
    OPEN_PR = "open_pr"
    WAIT_FOR_CI = "wait_for_ci"
    VERIFY = "verify"
    REGISTER_OUTCOME_OBSERVATION = "register_outcome_observation"
    WAIT = "wait"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class _OpenPrBinding:
    """The attempt id the ``open_pr`` node runs under and the approval it is bound to, if any."""

    attempt_id: str
    approval: dict[str, object] | None
    approval_round: int


@dataclass(frozen=True, slots=True)
class Stage1Progression:
    """The next Stage 1 action and the durable run it was derived from."""

    action: Stage1ProgressionAction
    run: Stage1Run
    hold: ExecutionHold | None = None
    observed_host_version: str | None = None
    """The host version the binding check observed while deriving this action.

    None when the run needs no native control or the host could not be probed.
    """


@dataclass(frozen=True, slots=True)
class _BindingCheck:
    """A binding hold verdict and the host version observed while reaching it."""

    hold: ExecutionHold | None
    observed_host_version: str | None = None


@dataclass(frozen=True, slots=True)
class Stage1ImplementationDispatch:
    """One implementation attempt registered with the coordinator runtime.

    ``fixture`` is the supervised OMP attempt when it was scheduled. It is ``None`` when the
    node stayed durably queued behind capacity or a dependency; ``wakeup`` then carries the
    recorded blocking reason, and a later ``advance`` redispatches the same operation.
    """

    task: HarnessTask
    fixture: DispatchedFixture | None
    result_path: Path
    wakeup: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Stage1RunService:
    """Create and read Stage 1 runs through the sole coordinator runtime."""

    runtime: CoordinatorRuntime
    ledger: LedgerService
    work_ledger: WorkLedgerPort | None = None
    pull_requests: PullRequestPort | None = None
    harness: SupervisedHarness | None = None
    outcomes: OutcomeCaptureService | None = None
    execution_catalog: ExecutionCatalog | None = None
    host_probe: Callable[[], InstalledHost | None] | None = None
    runner_admission: RunnerAdmissionPort | None = None
    isolated_command_runner: IsolatedCommandRunner | None = None
    branch_publisher: BranchPublicationPort | None = None
    behavioral_verifier: Callable[[Stage1RunRequest, str], Digest] | None = None

    def start(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1Run:
        """Persist an immutable run intent before any workflow side effect.

        A new run must bind the current check contracts: its manifest must be at a
        check-binding schema version, every check it binds must match the acceptance
        baseline, and request-supplied validation commands cannot invoke a shell. It must
        also bind an explicit execution selection. A run whose projection already exists is
        a restart and is read back unchanged, so a persisted run from before check
        contracts or execution selections keeps its original fingerprint and binding.
        """
        if (
            self.ledger.read_projection(
                aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=str(request.run.id)
            )
            is None
        ):
            if request.behavioral_checks is None:
                raise InvalidInputError(
                    "new runs require bound behavioral checks; rebuild with "
                    "enginery stage1 build-request"
                )
            if not request.behavioral_checks:
                raise HumanActionRequiredError(
                    "no behavioral checks were bound; add [acceptance] checks to "
                    f"{shlex.quote(str(request.repository_path / '.enginery/project.toml'))}, "
                    f"commit on {shlex.quote(request.base_branch)}, then rebuild "
                    "with enginery stage1 build-request and start the unbound run again",
                    details={"reason": "no_behavioral_checks"},
                )
            _require_current_check_contracts(request)
            _require_bound_selection(request)
            _require_bound_runner(request)
            _require_no_opaque_budget(request)
            require_explicit_isolated_validation(request)
            runner_hold = self._runner_hold(request)
            if runner_hold is not None:
                raise HumanActionRequiredError(
                    runner_hold.message, details=dict(runner_hold.details)
                )
            validate_against_catalog(request.execution_selection, catalog=self._current_catalog())
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
        """Derive at most one safe action, reporting a drifted catalog binding as a hold.

        A hold never rebinds the run: its original selection and catalog digest stay as
        recorded. A drifted run's advancing actions become HOLD; a human wait or idle
        observation keeps its action but still carries the hold diagnostic so it is shown.
        """
        progression = self._derive_progression(run_id)
        check = self._binding_check(progression.run.request)
        progression = replace(progression, observed_host_version=check.observed_host_version)
        hold = check.hold
        if hold is None:
            return progression
        if progression.action in {
            Stage1ProgressionAction.WAIT,
            Stage1ProgressionAction.AWAIT_HUMAN_REVIEW,
            Stage1ProgressionAction.BLOCKED,
        }:
            return replace(progression, hold=hold)
        return replace(progression, action=Stage1ProgressionAction.HOLD, hold=hold)

    def _current_catalog(self) -> ExecutionCatalog:
        return self.execution_catalog or load_execution_catalog()

    def _catalog_hold(self, request: Stage1RunRequest) -> ExecutionHold | None:
        return detect_catalog_drift(request.execution_selection, current=self._current_catalog())

    def _held_runs(self) -> dict[str, str]:
        """Map every Stage 1 run whose catalog binding has drifted to its hold message.

        Computed from durable run requests and the current catalog only, never from a host
        probe, so queued nodes of a held run are left out of the fair plan and cannot strand
        capacity another run could use. Host-fingerprint holds need a probe and stay a
        manual case. A run projection that is not a Stage 1 request has no catalog binding.
        """
        held: dict[str, str] = {}
        catalog = self._current_catalog()
        for projection in self.ledger.list_projections(aggregate_type=RUN_AGGREGATE_TYPE):
            try:
                run = _run_from_state(
                    projection.state, aggregate_version=projection.aggregate_version
                )
            except (InvalidInputError, InternalInvariantViolationError):
                continue
            hold = detect_catalog_drift(run.request.execution_selection, current=catalog)
            if hold is not None:
                held[projection.aggregate_id] = hold.message
        return held

    def _binding_check(self, request: Stage1RunRequest) -> _BindingCheck:
        """Return a catalog drift hold, else a runner or host fingerprint drift hold, else none.

        A trusted-local run probes the host only when it selected a native control, so a
        host-default run never depends on the installed version. An isolated run always probes
        the harness inside its pinned image and holds when it is not the catalog's tested
        executable and protocol, whether or not a native control was selected: the worker's
        version and protocol are part of what the runner receipt must prove. The version that
        probe observed is returned with the verdict instead of being discarded, so a
        diagnostic can show it.
        """
        if request.behavioral_checks == ():
            return _BindingCheck(
                ExecutionHold(
                    reason=HoldReason.NO_BEHAVIORAL_CHECKS,
                    message=(
                        f"run {request.run.id} has no behavioral checks and cannot be rebound; "
                        "add [acceptance] checks to "
                        f"{shlex.quote(str(request.repository_path / '.enginery/project.toml'))}, "
                        f"commit on {shlex.quote(request.base_branch)}, then supersede this run "
                        "with enginery stage1 build-request using a new --run-id and start "
                        "the resulting request"
                    ),
                )
            )
        hold = self._catalog_hold(request)
        if hold is not None:
            return _BindingCheck(hold)
        runner_hold = self._runner_hold(request)
        if runner_hold is not None:
            return _BindingCheck(runner_hold)
        selection = request.execution_selection
        if selection.legacy:
            return _BindingCheck(None)
        probe = self.host_probe
        if probe is None and self.harness is not None:
            probe = self.harness.installed_host
        if request.runner.isolated_mode:
            return self._image_harness_check(selection, probe)
        if not selection.requires_native_controls or probe is None:
            return _BindingCheck(None)
        installed = probe()
        return _BindingCheck(
            detect_host_drift(selection, installed=installed),
            None if installed is None else installed.executable_version,
        )

    def _image_harness_check(
        self,
        selection: ExecutionSelection,
        probe: Callable[[], InstalledHost | None] | None,
    ) -> _BindingCheck:
        """Hold unless the harness in the pinned image is the catalog's tested one.

        The image pins exactly the version the catalog tested, so a version merely inside
        the accepted range does not qualify an isolated run; the probed version is returned
        with the verdict so a diagnostic can show it.
        """
        if probe is None:
            return _BindingCheck(
                ExecutionHold(
                    reason=HoldReason.RUNNER_UNQUALIFIED,
                    message=(
                        "the run requires the isolated runner but the harness inside its image "
                        "cannot be probed; it is held, not run unverified"
                    ),
                    details={"harness": selection.harness},
                )
            )
        host = self._current_catalog().host(selection.harness)
        try:
            installed = probe()
        except (MissingPrerequisiteError, OperationTimeoutError) as error:
            return _BindingCheck(
                ExecutionHold(
                    reason=HoldReason.RUNNER_UNQUALIFIED,
                    message=(
                        f"the harness inside the pinned runner image could not be probed: "
                        f"{error}; the run is held, not run unverified"
                    ),
                    details={"harness": selection.harness},
                )
            )
        observed = None if installed is None else installed.executable_version
        if installed is not None and host.matches_tested(installed):
            return _BindingCheck(None, observed)
        return _BindingCheck(
            ExecutionHold(
                reason=HoldReason.RUNNER_FINGERPRINT_DRIFT,
                message=(
                    f"the {selection.harness} inside the pinned runner image is not the "
                    "tested executable and protocol the execution catalog names; the run is "
                    "held and its original binding is retained. Rebuild the runner image from "
                    "the current recipe or restore the catalog it was bound to"
                ),
                details={
                    "harness": selection.harness,
                    **host.version.details(),
                    "catalog_protocol_fingerprint": host.protocol_fingerprint,
                    "image_executable_version": "unobservable" if observed is None else observed,
                    "image_protocol_fingerprint": (
                        "unobservable" if installed is None else installed.protocol_fingerprint
                    ),
                },
            ),
            observed,
        )

    def _runner_hold(self, request: Stage1RunRequest) -> ExecutionHold | None:
        """Return a hold unless an isolated selection is currently admitted by a qualified runner.

        A run bound to the isolated profile is never run in another mode: with no runner
        admission configured, or one that does not qualify the pinned engine, image and
        profile, the run is held and its recorded binding is retained.
        """
        if not request.runner.isolated_mode:
            return None
        if self.runner_admission is None:
            return ExecutionHold(
                reason=HoldReason.RUNNER_UNQUALIFIED,
                message=(
                    "the run requires the isolated runner but no runner admission is "
                    "configured; it is held, not run in trusted-local mode"
                ),
                details={"trust_mode": request.runner.trust_mode.value},
            )
        return self.runner_admission.admit(request.runner).hold

    def _require_isolated_runner(self) -> IsolatedCommandRunner:
        if self.isolated_command_runner is None:
            raise HumanActionRequiredError(
                "isolated validation requires the isolated runner; it is not configured, and "
                "validation is never run on the host instead"
            )
        return self.isolated_command_runner

    def _recorded_execution_hold(
        self, implement_state: Mapping[str, object]
    ) -> ExecutionHold | None:
        attempt = self.ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=_string(implement_state, "attempt_id")
        )
        if attempt is None:
            return None
        result = attempt.state.get("result")
        raw = result.get("execution_hold") if isinstance(result, Mapping) else None
        return None if raw is None else ExecutionHold.from_state(raw)

    def _require_executable(self, request: Stage1RunRequest) -> None:
        """Reject a request that is not the durable binding or whose binding drifted."""
        durable = self.read(request.run.id).request
        if durable.digest != request.digest:
            raise StaleEvidenceError(
                "Stage 1 request does not match the durable run binding",
                details={
                    "run_id": str(request.run.id),
                    "bound_request_digest": str(durable.digest),
                    "supplied_request_digest": str(request.digest),
                },
            )
        hold = self._binding_check(durable).hold
        if hold is not None:
            raise HumanActionRequiredError(hold.message, details=dict(hold.details))
        if not durable.execution_selection.legacy:
            validate_against_catalog(durable.execution_selection, catalog=self._current_catalog())

    def _derive_progression(self, run_id: RunId) -> Stage1Progression:
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

        implement_state = self._node_state_optional(run_id, "implement")
        if implement_state is None:
            return Stage1Progression(Stage1ProgressionAction.IMPLEMENT, run)
        implementation_status = implement_state.get("status")
        if implementation_status in {"failed", "cancelled", "blocked"}:
            recorded = self._recorded_execution_hold(implement_state)
            if recorded is not None:
                return Stage1Progression(Stage1ProgressionAction.HOLD, run, recorded)
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        if implementation_status == "queued":
            return Stage1Progression(Stage1ProgressionAction.DISPATCH_QUEUED, run)
        if implementation_status == "running" and self._cancel_in_progress(str(run_id)):
            # The operator's cancel wins over a result that landed after it was requested.
            return self._running_worker_progression(run)
        if implementation_status != "passed":
            result_path = _implementation_result_path(
                run.request, operation_id=OperationId(_string(implement_state, "operation_id"))
            )
            if result_path.is_file():
                if implementation_status == "running" and self._worker_still_exiting(run):
                    return Stage1Progression(Stage1ProgressionAction.WAIT, run)
                return Stage1Progression(Stage1ProgressionAction.COLLECT_IMPLEMENTATION, run)
            if implementation_status == "running":
                return self._running_worker_progression(run)
            return Stage1Progression(Stage1ProgressionAction.WAIT, run)
        generation = _generation(_string(implement_state, "attempt_id"))

        validate_state = self._node_state_optional(run_id, "validate")
        if (
            validate_state is None
            or _generation(_string(validate_state, "attempt_id")) != generation
        ):
            return Stage1Progression(Stage1ProgressionAction.VALIDATE, run)
        if validate_state.get("status") == "blocked":
            # A hold such as a missing validation tool is not a verdict a human can approve.
            return Stage1Progression(Stage1ProgressionAction.BLOCKED, run)
        if validate_state.get("status") != "passed" or (
            not run.request.runner.legacy and "validation_artifact_digest" not in validate_state
        ):
            # A validation a human approved without it ever running is not a pass: it carries
            # no artifact, so it never reaches review or publication.
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)

        review_state = self._node_state_optional(run_id, "review")
        if review_state is None or _generation(_string(review_state, "attempt_id")) != generation:
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        if self._review_outcome(run_id) is not ReviewOutcome.APPROVED:
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)

        open_pr_state = self._node_state_optional(run_id, "open_pr")
        if open_pr_state is None or _generation(_string(open_pr_state, "attempt_id")) != generation:
            return Stage1Progression(Stage1ProgressionAction.OPEN_PR, run)
        if open_pr_state.get("status") == "queued":
            return Stage1Progression(Stage1ProgressionAction.OPEN_PR, run)
        if (
            open_pr_state.get("status") == "passed"
            and open_pr_state.get("operator_decision") == "approved"
            and "pull_request_number" not in open_pr_state
        ):
            # The operator approved a held publication: the branch is not published and no pull
            # request exists yet, so the step is performed now, never skipped.
            return Stage1Progression(Stage1ProgressionAction.OPEN_PR, run)
        if open_pr_state.get("status") != "passed":
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)

        wait_for_ci_state = self._node_state_optional(run_id, "wait_for_ci")
        if (
            wait_for_ci_state is None
            or _generation(_string(wait_for_ci_state, "attempt_id")) != generation
        ):
            return Stage1Progression(Stage1ProgressionAction.WAIT_FOR_CI, run)
        if wait_for_ci_state.get("status") == "queued":
            return Stage1Progression(Stage1ProgressionAction.WAIT_FOR_CI, run)
        if (
            wait_for_ci_state.get("status") == "failed"
            and wait_for_ci_state.get("pull_request_outcome") == PullRequestOutcome.BLOCKED.value
        ):
            # A genuine required-check failure on our own pushed head takes the same
            # bounded repair edge as an actionable review finding: a fresh implementation
            # generation when budget remains, otherwise escalation with the failure
            # attached. A superseded or otherwise ambiguous outcome still escalates
            # unconditionally, since the divergent subject was not necessarily caused by
            # this run's own implementation.
            action = (
                Stage1ProgressionAction.IMPLEMENT
                if generation - _generation(run.request.implementation.attempt_id)
                < run.request.repair_limit
                else Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
            )
            return Stage1Progression(action, run)
        if wait_for_ci_state.get("status") != "passed":
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)

        verify_state = self._node_state_optional(run_id, "verify")
        if verify_state is None or _generation(_string(verify_state, "attempt_id")) != generation:
            return Stage1Progression(Stage1ProgressionAction.VERIFY, run)
        if verify_state.get("status") == "queued":
            return Stage1Progression(Stage1ProgressionAction.VERIFY, run)
        if verify_state.get("status") == "failed" and verify_state.get("behavioral_findings"):
            action = (
                Stage1ProgressionAction.IMPLEMENT
                if generation - _generation(run.request.implementation.attempt_id)
                < run.request.repair_limit
                else Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
            )
            return Stage1Progression(action, run)
        if verify_state.get("status") != "passed":
            return Stage1Progression(Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, run)
        if self.outcomes is not None and not self._outcome_observation_registered(run_id):
            return Stage1Progression(Stage1ProgressionAction.REGISTER_OUTCOME_OBSERVATION, run)
        return Stage1Progression(Stage1ProgressionAction.WAIT, run)

    def _outcome_observation_registered(self, run_id: RunId) -> bool:
        assert self.outcomes is not None
        registered = self.outcomes.read_observation(
            observation_id_for(run_id, OutcomeKind.MERGE_RESULT)
        )
        return registered is not None

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
        elif progression.action is Stage1ProgressionAction.DISPATCH_QUEUED:
            self.redispatch_queued_implementation(
                request,
                now=now,
                heartbeat_window=heartbeat_window,
                lease_window=lease_window,
                limits=limits,
            )
        elif progression.action is Stage1ProgressionAction.RECONCILE_WORKER:
            self.reconcile_exited_worker(request, now=now, heartbeat_window=heartbeat_window)
        elif progression.action is Stage1ProgressionAction.COLLECT_IMPLEMENTATION:
            self.collect_implementation(
                request,
                now=now,
                heartbeat_window=heartbeat_window,
            )
        elif progression.action is Stage1ProgressionAction.VALIDATE:
            self.validate_implementation(request, now=now, heartbeat_window=heartbeat_window)
        elif progression.action is Stage1ProgressionAction.OPEN_PR:
            self.open_pull_request(request, now=now, heartbeat_window=heartbeat_window)
        elif progression.action is Stage1ProgressionAction.WAIT_FOR_CI:
            self.wait_for_ci(request, now=now, heartbeat_window=heartbeat_window)
        elif progression.action is Stage1ProgressionAction.VERIFY:
            self.verify_merge_ready(request, now=now, heartbeat_window=heartbeat_window)
        elif progression.action is Stage1ProgressionAction.REGISTER_OUTCOME_OBSERVATION:
            self.register_outcome_observation(request, now=now)
        return self.next_action(run_id)

    def qualify(
        self,
        request: Stage1RunRequest,
        *,
        external_reference: str,
        applicable_criteria: tuple[bool, ...],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> WorkQualification:
        """Verify the source binding before recording a durable qualification node."""
        snapshot = self._require_work_ledger().fetch(external_reference)
        if (
            snapshot.source_revision != request.work_snapshot.source_revision
            or snapshot.bound_digest != request.work_snapshot.bound_digest
        ):
            raise StaleEvidenceError(
                "Stage 1 source snapshot no longer matches the run request",
                details={
                    "expected_revision": request.work_snapshot.source_revision,
                    "observed_revision": snapshot.source_revision,
                    "expected_digest": str(request.work_snapshot.bound_digest),
                    "observed_digest": str(snapshot.bound_digest),
                },
            )
        self.start(request, now=now, heartbeat_window=heartbeat_window)
        self._require_executable(request)
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
        return Stage1QualificationExecutor(runtime=self.runtime).qualify(
            dispatch=dispatch,
            snapshot=snapshot,
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
        self._require_executable(request)
        _require_no_opaque_budget(request)
        self._require_passed_node(request.run.id, "qualify")
        existing = self._node_state_optional(request.run.id, "implement")
        if existing is None:
            attempt_id = request.implementation.attempt_id
            operation_id = request.implementation.operation_id
            repair_context = None
        else:
            if existing.get("status") not in {"passed", "failed", "cancelled", "blocked"}:
                raise ExternalConflictError(
                    "Stage 1 implementation repair requires a terminal prior attempt"
                )
            wait_state = self._node_state(request.run.id, "wait_for_ci")
            verify_state = self._node_state_optional(request.run.id, "verify")
            if (
                verify_state is not None
                and verify_state.get("status") == "failed"
                and verify_state.get("behavioral_findings")
                and _generation(_string(verify_state, "attempt_id"))
                == _generation(_string(existing, "attempt_id"))
            ):
                wait_state = verify_state
            elif (
                wait_state.get("status") != "failed"
                or wait_state.get("pull_request_outcome") != PullRequestOutcome.BLOCKED.value
            ):
                raise ExternalConflictError(
                    "Stage 1 implementation repair requires failed CI or behavioral assertions"
                )
            generation = _generation(_string(existing, "attempt_id")) + 1
            if generation - _generation(request.implementation.attempt_id) > request.repair_limit:
                raise ExternalConflictError(
                    "Stage 1 implementation repair exceeds the configured repair limit"
                )
            attempt_id = implementation_attempt_id(str(request.run.id), generation)
            operation_id = OperationId.derive(
                run_id=request.run.id,
                node_id=NodeId("implement"),
                side_effect_kind="repair_implementation",
                target_scope=request.repository_id,
                ordinal=generation,
            )
            repair_context = _repair_context(wait_state)
        base_revision = self._implementation_base(request)
        task = _implementation_task(
            request,
            attempt_id=attempt_id,
            operation_id=operation_id,
            repair_context=repair_context,
            base_revision=base_revision,
        )
        result_path = _implementation_result_path(request, operation_id=operation_id)
        dispatch = WorkflowDispatch(
            _fixture_dispatch(
                request,
                base_revision=base_revision,
                node_id="implement",
                attempt_id=attempt_id,
                operation_id=str(operation_id),
                command=self._require_harness().supervised_command(task, result_path=result_path),
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
            run_ids=(str(request.run.id),),
            held_runs=self._held_runs(),
        )
        return Stage1ImplementationDispatch(
            task=task,
            fixture=_scheduled_implementation(
                tick.dispatched,
                run_id=request.run.id,
                attempt_id=attempt_id,
                operation_id=operation_id,
            ),
            result_path=result_path,
            wakeup=self._queued_wakeup(request.run.id),
        )

    def redispatch_queued_implementation(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
        lease_window: timedelta,
        limits: SchedulingLimits,
    ) -> DispatchedFixture | None:
        """Re-evaluate a durably queued implementation and dispatch it once if now eligible.

        The queued node keeps its original attempt and operation identity: this registers
        nothing, only lets the coordinator re-derive readiness from the ledger. The same
        binding checks as the first dispatch run first, so a drifted run is held rather than
        launched, and the tick is scoped to this run so it can never launch another run's
        node under a binding checked here. Returns ``None`` when the node stays queued; its
        blocking reason and evaluation count are recorded on the runtime node.
        """
        self._require_executable(request)
        self._require_passed_node(request.run.id, "qualify")
        queued = self._node_state(request.run.id, "implement")
        if queued.get("status") != "queued":
            raise ExternalConflictError(
                "Stage 1 queued redispatch requires a queued implementation",
                details={"run_id": str(request.run.id), "status": str(queued.get("status"))},
            )
        tick = self.runtime.tick(
            now=now,
            heartbeat_window=heartbeat_window,
            lease_window=lease_window,
            limits=limits,
            run_ids=(str(request.run.id),),
            held_runs=self._held_runs(),
        )
        return _scheduled_implementation(
            tick.dispatched,
            run_id=request.run.id,
            attempt_id=_string(queued, "attempt_id"),
            operation_id=OperationId(_string(queued, "operation_id")),
        )

    def _running_worker_progression(self, run: Stage1Run) -> Stage1Progression:
        """Decide what a running implementation node with no result file is waiting on.

        A worker proven gone on this host -- its pid is free, held by another process, or the
        host rebooted since it launched -- is reconciled to a bounded state; the reconcile step
        looks once more for a result the worker wrote after this was derived. A worker that is
        alive keeps the run waiting, including after the coordinator epoch that launched it
        lapsed: with no daemon that is the normal state between ``watch --advance`` calls, and
        it is only reported as ``lease_epoch_active: false`` in the worker report. An
        interrupted cancel is finished. Whatever cannot be proven -- a worker recorded on
        another machine, or an unreadable identity -- is an explicit hold with its reason,
        never a silent wait.
        """
        run_id = str(run.request.run.id)
        record = self.ledger.read_process_manager_state(
            process_manager_name="worker-supervisor", state_key=f"{run_id}:implement"
        )
        if record is None:
            return Stage1Progression(Stage1ProgressionAction.WAIT, run)
        assessment = assess_worker(record.state)
        liveness = assessment.liveness
        if liveness in GONE:
            return Stage1Progression(Stage1ProgressionAction.RECONCILE_WORKER, run)
        if liveness is WorkerLiveness.ALIVE:
            if record.state.get("status") == "termination_requested":
                return Stage1Progression(Stage1ProgressionAction.RECONCILE_WORKER, run)
            return Stage1Progression(Stage1ProgressionAction.WAIT, run)
        return Stage1Progression(
            Stage1ProgressionAction.HOLD,
            run,
            ExecutionHold(
                reason=HoldReason.WORKER_IDENTITY_UNPROVABLE,
                message=(
                    "the implementation worker's liveness cannot be proven from this host "
                    f"({liveness.value}). There is no supported release for this in this "
                    "release: the node keeps its slot until the worker is dealt with on the "
                    "machine that launched it"
                ),
                details={"liveness": liveness.value},
            ),
        )

    def _worker_still_exiting(self, run: Stage1Run) -> bool:
        """Whether a worker that already published its result is provably still running.

        A worker writes its result and only then finishes shutting down. The supervisor
        refuses to record a result until it has observed the worker's exit, so collecting in
        that window can only fail with a conflict; the run waits for the exit instead. Only a
        worker this host proves alive waits: one that is gone, or whose liveness cannot be
        proven, is collected as before, where the supervisor records the exit or refuses with
        its own explicit reason. A worker that is a child of this process and has already
        exited is reaped here, since it would otherwise stay visible as running until someone
        waited for it.
        """
        record = self.ledger.read_process_manager_state(
            process_manager_name="worker-supervisor", state_key=f"{run.request.run.id}:implement"
        )
        if record is None:
            return False
        assessment = assess_worker(record.state)
        if assessment.liveness is not WorkerLiveness.ALIVE or assessment.identity is None:
            return False
        try:
            reaped, _ = os.waitpid(assessment.identity.pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return reaped != assessment.identity.pid

    def _cancel_in_progress(self, run_id: str) -> bool:
        record = self.ledger.read_process_manager_state(
            process_manager_name="worker-supervisor", state_key=f"{run_id}:implement"
        )
        return record is not None and record.state.get("status") == "termination_requested"

    def _implementation_result_exists(self, request: Stage1RunRequest) -> bool:
        implement_state = self._node_state(request.run.id, "implement")
        return _implementation_result_path(
            request, operation_id=OperationId(_string(implement_state, "operation_id"))
        ).is_file()

    def reconcile_exited_worker(
        self, request: Stage1RunRequest, *, now: datetime, heartbeat_window: timedelta
    ) -> None:
        """Resolve a worker that is provably gone: collect its result, else fail the node.

        A result written after progression was derived (or by the very worker that then
        exited) is collected rather than discarded as a death. An interrupted cancel is
        finished as a cancellation by the runtime, before any result is considered: the
        operator's cancel wins over a late result.
        """
        self._require_executable(request)
        if not self._cancel_in_progress(str(request.run.id)) and (
            self._implementation_result_exists(request)
        ):
            self.collect_implementation(request, now=now, heartbeat_window=heartbeat_window)
            return
        self.runtime.fail_exited_worker(
            run_id=str(request.run.id),
            node_id="implement",
            now=now,
            heartbeat_window=heartbeat_window,
        )

    def _queued_wakeup(self, run_id: RunId) -> Mapping[str, object] | None:
        state = self._node_state_optional(run_id, "implement")
        wakeup = None if state is None else state.get("wakeup")
        return wakeup if isinstance(wakeup, dict) else None

    def collect_implementation(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> HarnessResult:
        """Claim the current epoch, then ingest a completed supervised OMP result."""
        self._require_executable(request)
        self.runtime.claim_epoch(now=now, heartbeat_window=heartbeat_window)
        implement_state = self._node_state(request.run.id, "implement")
        attempt_id = _string(implement_state, "attempt_id")
        operation_id = OperationId(_string(implement_state, "operation_id"))
        task = _implementation_task(
            request,
            attempt_id=attempt_id,
            operation_id=operation_id,
            repair_context=None,
            base_revision=self._implementation_base(request),
        )
        result_path = _implementation_result_path(request, operation_id=operation_id)
        return Stage1ImplementationExecutor(
            runtime=self.runtime,
            harness=self._require_harness(),
            manifest=request.manifest,
            head_branch=request.head_branch,
            catalog=self._current_catalog(),
        ).collect(
            dispatched=self.runtime.recover_dispatched(
                run_id=str(request.run.id),
                node_id="implement",
            ),
            task=task,
            now=now,
            result_path=result_path,
            launched_command=self.runtime.read_node_request(
                run_id=str(request.run.id), node_id="implement"
            ).command,
        )

    def validate_implementation(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1ValidationResult:
        """Run configured focused validation after the OMP result is durable.

        An isolated run validates inside the isolated runner against a fresh checkout of the
        verified head, made from the host-owned intake repository rather than from the worker's
        live workspace, so nothing the worker left uncommitted or untracked can influence the
        verdict. A new trusted-local run validates on the host with an explicit environment
        instead of the ambient one; a run persisted before environment allowlists keeps its
        original host-side validation.
        """
        self._require_executable(request)
        self._require_passed_node(request.run.id, "implement")
        generation = self._implement_generation(request.run.id)
        executor = Stage1ValidationExecutor(
            runtime=self.runtime,
            artifact_store=self._require_harness().artifact_store,
        )
        dispatch = WorkflowNodeDispatch(
            _fixture_dispatch(
                request,
                node_id="validate",
                attempt_id=f"validate-{generation}",
                operation_id=str(
                    OperationId.derive(
                        run_id=request.run.id,
                        node_id=NodeId("validate"),
                        side_effect_kind="validation",
                        target_scope=request.repository_id,
                        ordinal=generation,
                    )
                ),
                command=request.validation_commands[0],
                dependencies=((str(request.run.id), "implement"),),
            ),
            request.manifest,
        )
        if request.runner.isolated_mode:
            isolated_runner = self._require_isolated_runner()
            head_revision = self._verified_head_revision(
                attempt_id=_string(self._node_state(request.run.id, "implement"), "attempt_id")
            )
            with verified_head_checkout(
                workspace_path=request.workspace_path, head_revision=head_revision
            ) as checkout:
                return replace(
                    executor, command_runner=MissingToolProbingRunner(isolated_runner)
                ).validate(
                    dispatch=dispatch,
                    commands=request.validation_commands,
                    now=now,
                    heartbeat_window=heartbeat_window,
                    working_directory=checkout,
                    validated_revision=head_revision,
                )
        if not request.runner.legacy:
            executor = replace(executor, command_runner=run_sanitized_validation_command)
        return executor.validate(
            dispatch=dispatch,
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
        packet_feedback_digest: str | None = None,
    ) -> Stage1ReviewResult:
        """Record an independent review only after passed validation.

        ``repair_attempt`` is the repair index of the implementation being reviewed (0 for the
        first attempt); the review node carries the implementation's own generation, so a run
        whose first attempt id ends in another number is reviewed under the same one.
        """
        self._require_executable(request)
        self._require_passed_node(request.run.id, "validate")
        self._require_validation_artifact(request)
        generation = _generation(request.implementation.attempt_id) + repair_attempt
        if generation != self._implement_generation(request.run.id):
            raise InvalidInputError(
                "the review's repair attempt is not the implementation's repair index",
                details={"repair_attempt": repair_attempt, "run_id": str(request.run.id)},
            )
        return Stage1ReviewExecutor(self.runtime).review(
            dispatch=WorkflowNodeDispatch(
                _fixture_dispatch(
                    request,
                    node_id="review",
                    attempt_id=(
                        f"review-{generation}" if packet_feedback_digest is None
                        else f"review-feedback-{packet_feedback_digest}-{generation}"
                    ),
                    operation_id=str(
                        OperationId.derive(
                            run_id=request.run.id,
                            node_id=NodeId("review"),
                            side_effect_kind="review",
                            target_scope=(
                                request.repository_id if packet_feedback_digest is None
                                else f"{request.repository_id}:{packet_feedback_digest}"
                            ),
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
            execution_selection_digest=_selection_digest(request),
            now=now,
            heartbeat_window=heartbeat_window,
            packet_feedback=packet_feedback_digest is not None,
        )

    def open_pull_request(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> None:
        """Publish the verified branch, persist a PR intent, then create or reconcile one PR.

        A new run's worker only commits locally and holds no remote or credential: the operator
        side publishes the verified branch here, before the pull request, constrained to the
        remote head it expects. A run persisted before operator publication keeps the contract
        its worker was launched under, which pushed the branch itself, and publishes nothing.

        A branch that changes a protected path (CI configuration, code ownership, evaluator or
        holdout material) is held before anything is pushed: the node waits for the operator's
        ``stage1 approve``/``stage1 reject`` decision, and only a recorded approval of exactly
        that matched set lets the next call publish it.
        """
        self._require_executable(request)
        self._require_passed_node(request.run.id, "review")
        self._require_validation_artifact(request)
        if self._review_outcome(request.run.id) is not ReviewOutcome.APPROVED:
            raise MissingPrerequisiteError("Stage 1 requires an approved independent review")
        publisher = None if request.runner.legacy else self._require_branch_publisher()
        generation = self._implement_generation(request.run.id)
        # The pull-request operation id is one logical side effect for the whole run:
        # it stays constant across repair generations so a repaired implementation
        # reconciles the same pull request instead of ambiguously creating a new one.
        operation_id = OperationId.derive(
            run_id=request.run.id,
            node_id=NodeId("open_pr"),
            side_effect_kind="pull_request",
            target_scope=request.repository_id,
            ordinal=0,
        )
        binding = (
            _OpenPrBinding(attempt_id=f"open-pr-{generation}", approval=None, approval_round=0)
            if publisher is None
            else self._open_pr_binding(request, generation=generation)
        )
        approval = binding.approval
        dispatch = WorkflowNodeDispatch(
            _fixture_dispatch(
                request,
                node_id="open_pr",
                attempt_id=binding.attempt_id,
                operation_id=str(operation_id),
                command=("open_pr",),
                dependencies=((str(request.run.id), "review"),),
            ),
            request.manifest,
        )
        epoch = self.runtime.register_node(
            dispatch=dispatch,
            now=now,
            heartbeat_window=heartbeat_window,
            # The approved digest and reason travel with the re-registered node, so a retry or a
            # crash after the approval still compares against exactly what was approved.
            extra=(
                None
                if approval is None
                else {
                    "publication_approval": dict(approval),
                    "approval_round": binding.approval_round,
                }
            ),
        )
        publication: dict[str, object] = {}
        if publisher is not None:
            publication["protected_publication"] = self._require_protected_publication_decision(
                request,
                publisher,
                generation=generation,
                approval=approval,
                epoch=epoch.epoch,
                now=now,
            )
            if approval is not None:
                publication["publication_approval"] = dict(approval)
            published = self._publish_verified_branch(request, publisher, generation=generation)
            publication["published_revision"] = published.revision
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
                **publication,
            },
        )

    def _publish_verified_branch(
        self,
        request: Stage1RunRequest,
        publisher: BranchPublicationPort,
        *,
        generation: int,
    ) -> PublishedBranch:
        """Publish the verified head of the current attempt, expecting the previous publication.

        The first publication of the run, whatever the trailing number of its first attempt's id,
        expects the remote branch to be absent. A repair generation expects it at exactly the head
        the previous generation's verified attempt published; that attempt is mapped back to the
        id it was really dispatched under and its head read from the ledger-recorded verified
        result, which survives the open-pull-request node being re-registered. A remote branch
        at any other revision is an external conflict, never overwritten.
        """
        head_revision = self._verified_head_revision(
            attempt_id=_string(self._node_state(request.run.id, "implement"), "attempt_id")
        )
        expected_remote_revision = (
            None
            if generation == _generation(request.implementation.attempt_id)
            else self._verified_head_revision(
                attempt_id=_implementation_attempt_for(request, generation - 1)
            )
        )
        published = publisher.publish(
            BranchPublicationRequest(
                repository_id=request.repository_id,
                branch=request.head_branch,
                head_revision=head_revision,
                expected_remote_revision=expected_remote_revision,
                operation_id=OperationId.derive(
                    run_id=request.run.id,
                    node_id=NodeId("open_pr"),
                    side_effect_kind="branch_publication",
                    target_scope=request.repository_id,
                    ordinal=generation,
                ),
            )
        )
        if published.branch != request.head_branch or published.revision != head_revision:
            raise ExternalConflictError(
                "the branch publisher did not report the verified branch and revision",
                details={
                    "branch": request.head_branch,
                    "head_revision": head_revision,
                    "published_revision": published.revision,
                },
            )
        return published

    def _verified_head_revision(self, *, attempt_id: str) -> str:
        """Return the head revision the host verified when it collected one attempt."""
        projection = self.ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=attempt_id
        )
        result = None if projection is None else projection.state.get("result")
        head_revision = result.get("head_revision") if isinstance(result, Mapping) else None
        if not isinstance(head_revision, str) or not head_revision.strip():
            raise MissingPrerequisiteError(
                "Stage 1 requires the verified head revision of the implementation attempt",
                details={"attempt_id": attempt_id},
            )
        return head_revision

    def _open_pr_binding(self, request: Stage1RunRequest, *, generation: int) -> _OpenPrBinding:
        """Return the attempt id and the operator's approval the ``open_pr`` node runs under.

        The only approval is the typed ``stage1 approve`` decision on the held ``open_pr`` node,
        read back from its durable state: a node that is passed by a decision, carries the held
        record and has not created a pull request. Consuming it re-registers the node under an
        attempt id that names the approval and carries the approved digest and reason in its
        own state, so a retry or a crash after the approval is still bound to exactly the set
        that was approved and is compared again every time. A node that is still waiting, or
        queued without an approval, keeps its attempt id. A recorded rejection is final and is
        never overridden.
        """
        default = _OpenPrBinding(
            attempt_id=f"open-pr-{generation}", approval=None, approval_round=0
        )
        state = self._node_state_optional(request.run.id, "open_pr")
        if state is None:
            return default
        attempt_id = _string(state, "attempt_id")
        if _generation(attempt_id) != generation:
            return default
        status = state.get("status")
        decision = state.get("operator_decision")
        if status == "failed" and decision == "rejected":
            raise ExternalConflictError(
                "the operator rejected publishing this branch, which changes protected paths",
                details={"run_id": str(request.run.id), "reason": str(state.get("reason"))},
            )
        approved_attempt = attempt_id.startswith("open-pr-approved-")
        previous_round = _approval_round(state) if approved_attempt else 0
        held = state.get("protected_publication")
        if (
            status == "passed"
            and decision == "approved"
            and isinstance(held, Mapping)
            and "pull_request_number" not in state
        ):
            # A decision on a node that was held again after an approval stopped matching is a
            # new approval: it is consumed under a fresh attempt id, never the spent one.
            approval_round = previous_round + 1
            return _OpenPrBinding(
                attempt_id=(
                    f"open-pr-approved-{generation}"
                    if approval_round == 1
                    else f"open-pr-approved-r{approval_round}-{generation}"
                ),
                approval={
                    "operator_decision": "approved",
                    "reason": str(state.get("reason")),
                    "approved_protected_publication_digest": str(held.get("digest")),
                },
                approval_round=approval_round,
            )
        if status == "queued" and approved_attempt:
            carried = state.get("publication_approval")
            if isinstance(carried, Mapping):
                return _OpenPrBinding(
                    attempt_id=attempt_id,
                    approval=dict(carried),
                    approval_round=previous_round,
                )
        if status in {"queued", "awaiting_human"}:
            return _OpenPrBinding(attempt_id=attempt_id, approval=None, approval_round=0)
        return default

    def _require_protected_publication_decision(
        self,
        request: Stage1RunRequest,
        publisher: BranchPublicationPort,
        *,
        generation: int,
        approval: Mapping[str, object] | None,
        epoch: int,
        now: datetime,
    ) -> dict[str, object]:
        """Return the protected-path record of the verified branch, holding it unless approved.

        The changed paths are read by the host from its own verified repository, never from a
        worker claim, and matched against the protected set. A match with no recorded approval,
        or whose approved digest differs from the current one, puts the ``open_pr`` node into a
        durable human wait and raises: nothing is pushed.
        """
        head_revision = self._verified_head_revision(
            attempt_id=_string(self._node_state(request.run.id, "implement"), "attempt_id")
        )
        reservation = self.runtime.read_workspace_reservation(request.repository_id)
        base_revision = (
            reservation.resolved_base_revision
            if reservation is not None and reservation.run_id == str(request.run.id)
            else request.run.base_revision
        )
        policy = ProtectedPathPolicy(request.protected_paths)
        changed = publisher.changed_paths(base_revision=base_revision, head_revision=head_revision)
        matched = policy.matched(changed)
        record: dict[str, object] = {
            "patterns": list(policy.patterns),
            "base_revision": base_revision,
            "head_revision": head_revision,
            "changed_path_count": len(changed),
            "matched_path_count": len(matched),
            "matched_paths": list(matched[:_MAX_RECORDED_PROTECTED_PATHS]),
            "digest": str(
                Digest.of_json(
                    {
                        "patterns": list(policy.patterns),
                        "base_revision": base_revision,
                        "head_revision": head_revision,
                        "matched_paths": list(matched),
                    }
                )
            ),
        }
        if not matched:
            return record
        lapsed = ""
        if approval is not None:
            # The digest is compared on every call, never only on the first one after the
            # decision: a retry or a crash resumes under exactly what was approved.
            if approval.get("approved_protected_publication_digest") == record["digest"]:
                return record
            lapsed = (
                "the approved protected-path publication no longer matches the verified branch "
                "or the protected set, so the approval is void; "
            )
        approve = _decision_command("approve", request)
        reject = _decision_command("reject", request)
        shown = ", ".join(matched[:5]) + (
            f" and {len(matched) - 5} more" if len(matched) > 5 else ""
        )
        reason = (
            f"{lapsed}the verified branch changes protected paths ({shown}); it is held and "
            f"nothing is published. Review the change, then publish it with `{approve}` or "
            f"refuse it with `{reject}`"
        )
        if self._node_status(request.run.id, "open_pr") == "queued":
            self.runtime.await_human_node(
                run_id=str(request.run.id),
                node_id="open_pr",
                epoch=epoch,
                now=now,
                reason=reason,
                extra={"protected_publication": record, "generation": generation},
            )
        raise HumanActionRequiredError(
            reason,
            details={
                "node_id": "open_pr",
                "matched_paths": list(matched[:_MAX_RECORDED_PROTECTED_PATHS]),
                "approve_command": approve,
                "reject_command": reject,
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

    def _node_state(self, run_id: RunId, node_id: str) -> Mapping[str, object]:
        projection = self.ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
            aggregate_id=f"{run_id}:{node_id}",
        )
        if projection is None:
            raise MissingPrerequisiteError(
                f"Stage 1 requires runtime node {node_id!r}",
                details={"run_id": str(run_id), "node_id": node_id},
            )
        return projection.state

    def _node_state_optional(self, run_id: RunId, node_id: str) -> Mapping[str, object] | None:
        projection = self.ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
            aggregate_id=f"{run_id}:{node_id}",
        )
        return None if projection is None else projection.state

    def _implement_generation(self, run_id: RunId) -> int:
        """Return the repair generation of the current durable implementation attempt."""
        return _generation(_string(self._node_state(run_id, "implement"), "attempt_id"))

    def _require_attempt_state(self, attempt_id: str) -> Mapping[str, object]:
        projection = self.ledger.read_projection(
            aggregate_type="node_attempt", aggregate_id=attempt_id
        )
        if projection is None:
            raise MissingPrerequisiteError(
                "Stage 1 merge-ready verification requires a durable implementation attempt",
                details={"attempt_id": attempt_id},
            )
        return projection.state

    def wait_for_ci(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> PullRequestOutcome:
        """Observe exact-head CI until it passes or produces a terminal failure."""
        self._require_executable(request)
        self._require_passed_node(request.run.id, "open_pr")
        open_pr = self._node_state(request.run.id, "open_pr")
        generation = self._implement_generation(request.run.id)
        pull_request_number = _integer(open_pr, "pull_request_number")
        expected_head_revision = _string(open_pr, "head_revision")
        requirements = PullRequestRequirements(
            expected_head_revision=expected_head_revision,
            required_checks=request.required_checks,
            require_approved_review=False,
        )
        dispatch = WorkflowNodeDispatch(
            _fixture_dispatch(
                request,
                node_id="wait_for_ci",
                attempt_id=f"wait-for-ci-{generation}",
                operation_id=str(
                    OperationId.derive(
                        run_id=request.run.id,
                        node_id=NodeId("wait_for_ci"),
                        side_effect_kind="ci_observation",
                        target_scope=request.repository_id,
                        ordinal=generation,
                    )
                ),
                command=("wait_for_ci",),
                dependencies=((str(request.run.id), "open_pr"),),
            ),
            request.manifest,
        )
        epoch = self.runtime.register_node(
            dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
        )
        evidence = self._require_pull_requests().evidence(pull_request_number)
        outcome = evaluate_pull_request(evidence, requirements)
        if outcome is PullRequestOutcome.WAITING:
            return outcome
        extra: dict[str, object] = {"pull_request_outcome": outcome.value}
        failed_checks = _failed_checks(evidence, request.required_checks)
        if failed_checks:
            extra["failed_checks"] = list(failed_checks)
        self.runtime.complete_node(
            run_id=str(request.run.id),
            node_id="wait_for_ci",
            epoch=epoch.epoch,
            now=now,
            outcome="passed" if outcome is PullRequestOutcome.MERGE_READY else "failed",
            extra=extra,
        )
        return outcome

    def verify_merge_ready(
        self,
        request: Stage1RunRequest,
        *,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1VerificationResult:
        """Double-read current subjects before recording a merge-ready evidence bundle."""
        self._require_executable(request)
        self._require_passed_node(request.run.id, "wait_for_ci")
        open_pr = self._node_state(request.run.id, "open_pr")
        generation = self._implement_generation(request.run.id)
        implementation = self._require_attempt_state(
            _string(self._node_state(request.run.id, "implement"), "attempt_id")
        )
        validation = self._node_state(request.run.id, "validate")
        behavioral_receipts: tuple[Digest, ...] = ()
        if request.behavioral_checks is not None:
            if self.behavioral_verifier is None:
                raise HumanActionRequiredError(
                    "behavioral acceptance is required but no trusted observer is configured; "
                    "use enginery stage1 watch with the run's --database, --owner and "
                    "--run-id options and --advance to compose the trusted observer",
                    details={"reason": "behavioral_verifier_unconfigured"},
                )
            try:
                behavioral_receipts = (
                    self.behavioral_verifier(request, _string(open_pr, "head_revision")),
                )
            except HumanActionRequiredError as error:
                if error.details.get("reason") != "behavioral_acceptance_failed":
                    raise
                findings = error.details.get("findings")
                if not isinstance(findings, (list, tuple)) or not findings:
                    raise
                dispatch = WorkflowNodeDispatch(
                    _fixture_dispatch(
                        request,
                        node_id="verify",
                        attempt_id=f"verify-{generation}",
                        operation_id=str(
                            OperationId.derive(
                                run_id=request.run.id,
                                node_id=NodeId("verify"),
                                side_effect_kind="publish_merge_ready",
                                target_scope=request.work_snapshot.work_item.external_reference,
                                ordinal=generation,
                            )
                        ),
                        command=("verify_merge_ready",),
                        dependencies=((str(request.run.id), "wait_for_ci"),),
                    ),
                    request.manifest,
                )
                epoch = self.runtime.register_node(
                    dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
                )
                self.runtime.complete_node(
                    run_id=str(request.run.id),
                    node_id="verify",
                    epoch=epoch.epoch,
                    now=now,
                    outcome="failed",
                    extra={"behavioral_findings": [str(finding) for finding in findings]},
                )
                if (
                    generation - _generation(request.implementation.attempt_id)
                    >= request.repair_limit
                ):
                    raise HumanActionRequiredError(
                        str(error),
                        details={
                            **error.details,
                            "repair": "The behavioral repair budget is exhausted; this run "
                            "cannot pass. Fix the behavior and start a new run: for factory "
                            "runs, change the issue snapshot and use enginery factory run "
                            "ISSUE --project PROJECT --supersede; for Stage 1 runs, rebuild "
                            "with enginery stage1 build-request --run-id NEW_RUN_ID "
                            "then enginery stage1 start --request REQUEST.",
                        },
                    ) from error
                raise
        verification_request = Stage1VerificationRequest(
            run_id=request.run.id,
            lifecycle_operation_id=OperationId.derive(
                run_id=request.run.id,
                node_id=NodeId("verify"),
                side_effect_kind="publish_merge_ready",
                target_scope=request.work_snapshot.work_item.external_reference,
                ordinal=generation,
            ),
            external_reference=request.work_snapshot.work_item.external_reference,
            issue_revision=request.work_snapshot.source_revision,
            issue_digest=str(request.work_snapshot.bound_digest),
            base_revision=request.run.base_revision,
            pull_request_number=_integer(open_pr, "pull_request_number"),
            requirements=PullRequestRequirements(
                expected_head_revision=_string(open_pr, "head_revision"),
                required_checks=request.required_checks,
                require_approved_review=False,
            ),
            implementation_artifacts=_digest_tuple(implementation, "artifact_references"),
            verification_artifacts=(
                _digest_from_string(_string(validation, "validation_artifact_digest")),
                *behavioral_receipts,
            ),
            execution_selection_digest=_selection_digest(request),
        )
        dispatch = WorkflowNodeDispatch(
            _fixture_dispatch(
                request,
                node_id="verify",
                attempt_id=f"verify-{generation}",
                operation_id=str(verification_request.lifecycle_operation_id),
                command=("verify_merge_ready",),
                dependencies=((str(request.run.id), "wait_for_ci"),),
            ),
            request.manifest,
        )
        epoch = self.runtime.register_node(
            dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
        )
        result = Stage1VerificationExecutor(
            work_ledger=self._require_work_ledger(),
            pull_requests=self._require_pull_requests(),
        ).verify(request=verification_request, observed_at=now)
        if result.outcome is PullRequestOutcome.WAITING:
            return result
        extra: dict[str, object] = {"pull_request_outcome": result.outcome.value}
        if result.evidence is not None:
            extra["evidence_digest"] = str(result.evidence.digest)
        if not request.execution_selection.legacy:
            extra["execution_selection_digest"] = str(request.execution_selection.digest)
        self.runtime.complete_node(
            run_id=str(request.run.id),
            node_id="verify",
            epoch=epoch.epoch,
            now=now,
            outcome="passed" if result.outcome is PullRequestOutcome.MERGE_READY else "failed",
            extra=extra,
        )
        return result

    def register_outcome_observation(self, request: Stage1RunRequest, *, now: datetime) -> None:
        """Register the pending post-merge-ready observations this run's
        subject still needs watched: whether its pull request eventually
        merges or closes unmerged, and whether its issue later reopens.
        Both stay durable and pending until a sweep or an explicit human
        capture resolves them, or their declared window elapses."""
        outcomes = self._require_outcomes()
        open_pr = self._node_state(request.run.id, "open_pr")
        work_item_id = request.run.work_item_id
        run_id = request.run.id
        outcomes.register_pending(
            work_item_id=work_item_id,
            run_id=run_id,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference=str(_integer(open_pr, "pull_request_number")),
            opened_at=now,
        )
        outcomes.register_pending(
            work_item_id=work_item_id,
            run_id=run_id,
            kind=OutcomeKind.REOPENED_ISSUE,
            subject_reference=request.work_snapshot.work_item.external_reference,
            opened_at=now,
        )

    def _require_outcomes(self) -> OutcomeCaptureService:
        if self.outcomes is None:
            raise MissingPrerequisiteError(
                "Stage 1 outcome observation registration requires outcomes"
            )
        return self.outcomes

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

    def _require_validation_artifact(self, request: Stage1RunRequest) -> None:
        """Refuse a passed validation that recorded no artifact: it never actually ran.

        A run persisted while a missing-tool hold could be approved carries such a node. The
        approval marks it passed, but only a validation that ran records its artifact digest.
        """
        if request.runner.legacy:
            return
        if "validation_artifact_digest" not in self._node_state(request.run.id, "validate"):
            raise MissingPrerequisiteError(
                "Stage 1 requires a recorded validation result, not an approved hold",
                details={"run_id": str(request.run.id), "node_id": "validate"},
            )

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

    def _implementation_base(self, request: Stage1RunRequest) -> str:
        """Return the base the implementation worker and its verification must share.

        An isolated worker clones only what the base reaches, so a branch or tag name does not
        resolve inside its clone. The base is therefore a full object id, resolved once: the
        first implementation dispatch binds it to the run's implementation node, and every later
        attempt (a repair generation, a retry, the collection of a result) reuses it, so a base
        name that has since moved cannot change what the run builds on. Before any dispatch the
        id an existing reservation holds is used, else the one the repository resolves now. A
        trusted-local run keeps the requested revision.
        """
        if not request.runner.isolated_mode:
            return request.run.base_revision
        if self._node_state_optional(request.run.id, "implement") is not None:
            bound = self.runtime.read_node_request(
                run_id=str(request.run.id), node_id="implement"
            ).base_revision
            if bound != request.run.base_revision:
                return bound
        reservation = self.runtime.read_workspace_reservation(request.repository_id)
        if (
            reservation is not None
            and reservation.run_id == str(request.run.id)
            and reservation.status in {"reserved", "materialized", "retained"}
        ):
            return reservation.resolved_base_revision
        return resolve_commit(request.repository_path, request.run.base_revision)

    def _require_work_ledger(self) -> WorkLedgerPort:
        if self.work_ledger is None:
            raise MissingPrerequisiteError("Stage 1 source-work ledger is not configured")
        return self.work_ledger

    def _require_pull_requests(self) -> PullRequestPort:
        if self.pull_requests is None:
            raise MissingPrerequisiteError("Stage 1 pull-request provider is not configured")
        return self.pull_requests

    def _require_harness(self) -> SupervisedHarness:
        if self.harness is None:
            raise MissingPrerequisiteError("Stage 1 harness is not configured")
        return self.harness

    def _require_branch_publisher(self) -> BranchPublicationPort:
        if self.branch_publisher is None:
            raise MissingPrerequisiteError(
                "Stage 1 branch publisher is not configured; the worker does not push, so the "
                "verified branch cannot be published"
            )
        return self.branch_publisher


def _implementation_task(
    request: Stage1RunRequest,
    *,
    attempt_id: str,
    operation_id: OperationId,
    repair_context: str | None,
    base_revision: str,
) -> HarnessTask:
    execution = request.implementation
    isolated = request.runner.isolated_mode
    # A run bound to a trust mode never lets its worker publish: the worker commits locally and
    # the operator side publishes the verified branch. Only a run persisted before trust modes
    # existed keeps the worker-pushes contract it was launched under.
    operator_publishes = not request.runner.legacy
    if repair_context is None:
        branch_constraint = (
            f"Create and work only on branch {request.head_branch!r} from "
            f"{base_revision!r}; "
            + (
                (
                    "commit the completed change locally. You are given no remote access and "
                    "no credentials: do not push; the operator publishes the verified branch."
                    if isolated
                    else "commit the completed change locally and do not push: the operator "
                    "publishes the verified branch."
                )
                if operator_publishes
                else "commit and push the completed change to origin."
            )
        )
    else:
        branch_constraint = (
            f"Continue only on the existing branch {request.head_branch!r}; do not create "
            f"a new branch. {repair_context} "
            + (
                "Commit the corrected change locally; do not push, the operator publishes it."
                if operator_publishes
                else "Commit and push the corrected change to origin."
            )
        )
    return HarnessTask(
        run_id=request.run.id,
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId(attempt_id),
        operation_id=operation_id,
        workspace_path=request.workspace_path,
        objective=request.work_snapshot.work_item.objective,
        acceptance_criteria=request.work_snapshot.work_item.acceptance_criteria,
        constraints=(*request.work_snapshot.work_item.constraints, branch_constraint),
        permitted_capabilities=execution.permitted_capabilities,
        evidence_requirements=execution.evidence_requirements,
        time_budget_seconds=execution.time_budget_seconds,
        cost_budget=execution.cost_budget,
        selection=request.execution_selection,
        head_branch=request.head_branch if isolated else None,
        base_revision=base_revision if isolated else None,
    )


def implementation_attempt_id(run_id: str, generation: int) -> str:
    """Derive ``<run-id>-implement-<generation>``, failing loudly if it is not a valid id.

    Attempt ids are ledger-wide aggregate ids with a bounded length. A run id too long to
    carry the suffix is rejected rather than truncated, which could collide with another run.
    """
    attempt_id = f"{run_id}-implement-{generation}"
    try:
        NodeAttemptId(attempt_id)
    except InvalidInputError as error:
        raise InvalidInputError(
            "run id is too long to derive a valid implementation attempt id; use a shorter run id",
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from error
    return attempt_id


def first_attempt_generation(attempt_id: str) -> int:
    """Return the generation a run's first implementation attempt id carries, or refuse the id.

    A run's generations are the trailing numbers of its implementation attempt ids: the first
    attempt's explicit id sets the starting one and every repair adds one. An id without a
    trailing number cannot start a run.
    """
    suffix = attempt_id.rsplit("-", 1)[-1]
    if not suffix.isdigit():
        raise InvalidInputError(
            "an implementation attempt id must end in -<number>",
            details={"attempt_id": attempt_id},
        )
    return int(suffix)


def _implementation_attempt_for(request: Stage1RunRequest, generation: int) -> str:
    """Return the attempt id the implementation of one generation was really dispatched under.

    The first generation is the run's own first attempt id, whatever number it ends in; every
    later generation is the derived repair id. A generation before the first one never existed.
    """
    first = _generation(request.implementation.attempt_id)
    if generation == first:
        return request.implementation.attempt_id
    if generation < first:
        raise InternalInvariantViolationError(
            "Stage 1 has no implementation attempt before its first generation",
            details={"run_id": str(request.run.id), "generation": generation},
        )
    return implementation_attempt_id(str(request.run.id), generation)


_MAX_RECORDED_PROTECTED_PATHS = 200


def _decision_command(decision: str, request: Stage1RunRequest) -> str:
    """Name the exact operator command that records ``decision`` on the held publication."""
    return (
        f"enginery stage1 {decision} --database <ledger> --owner <operator> "
        f"--run-id {shlex.quote(str(request.run.id))} --node-id open_pr "
        '--reason "<why>"'
    )


def _implementation_result_path(request: Stage1RunRequest, *, operation_id: OperationId) -> Path:
    name = f".enginery-omp-{Digest.of_bytes(str(operation_id).encode())}.json"
    if request.runner.isolated_mode:
        # The worker writes its result into the per-attempt output directory, the only
        # writable mount besides its task-local workspace, never into a host-owned path.
        return out_path_for(request.workspace_path) / name
    return request.workspace_path / name


def _scheduled_implementation(
    dispatched: tuple[DispatchedFixture, ...],
    *,
    run_id: RunId,
    attempt_id: str,
    operation_id: OperationId,
) -> DispatchedFixture | None:
    """Return the one launch of this exact implementation operation, or None if still queued."""
    matching = tuple(
        fixture
        for fixture in dispatched
        if fixture.lease.run_id == str(run_id)
        and fixture.lease.node_id == "implement"
        and fixture.lease.attempt_id == attempt_id
        and fixture.lease.operation_id == str(operation_id)
    )
    if len(matching) > 1:
        raise InternalInvariantViolationError(
            "a tick launched one implementation operation more than once",
            details={"run_id": str(run_id), "operation_id": str(operation_id)},
        )
    return matching[0] if matching else None


def _approval_round(state: Mapping[str, object]) -> int:
    """How many protected-path approvals the ``open_pr`` node has consumed; one when unrecorded."""
    recorded = state.get("approval_round")
    return recorded if isinstance(recorded, int) and not isinstance(recorded, bool) else 1


def _generation(attempt_id: str) -> int:
    """Parse the trailing repair generation encoded in a runtime node attempt id."""
    suffix = attempt_id.rsplit("-", 1)[-1]
    if not suffix.isdigit():
        raise InternalInvariantViolationError(
            "Stage 1 runtime node attempt id has an invalid generation suffix",
            details={"attempt_id": attempt_id},
        )
    return int(suffix)


def _failed_checks(
    evidence: PullRequestEvidence, required_checks: tuple[str, ...]
) -> tuple[dict[str, str | None], ...]:
    entries: list[dict[str, str | None]] = []
    for check in evidence.checks:
        if check.name not in required_checks:
            continue
        if check.status.lower() != "completed" or (
            check.conclusion is None or check.conclusion.lower() != "success"
        ):
            entries.append(
                {"name": check.name, "status": check.status, "conclusion": check.conclusion}
            )
    return tuple(entries)


def _repair_context(wait_state: Mapping[str, object]) -> str:
    findings = wait_state.get("behavioral_findings")
    if isinstance(findings, list) and findings:
        return "Repair the previous candidate's failing behavioral assertions: " + "; ".join(
            str(finding) for finding in findings
        )
    failed_checks = wait_state.get("failed_checks")
    if isinstance(failed_checks, list) and failed_checks:
        details = "; ".join(
            f"{entry.get('name')}: {entry.get('conclusion') or entry.get('status')}"
            for entry in failed_checks
            if isinstance(entry, dict)
        )
    else:
        outcome = wait_state.get("pull_request_outcome", "blocked")
        details = f"pull request outcome was {outcome!r}"
    return f"The previous attempt's pull request failed exact-head CI ({details})."


def _fixture_dispatch(
    request: Stage1RunRequest,
    *,
    node_id: str,
    attempt_id: str,
    operation_id: str,
    command: tuple[str, ...],
    dependencies: tuple[tuple[str, str], ...] = (),
    base_revision: str | None = None,
) -> FixtureDispatch:
    # Only the implement node launches a worker process; a new run launches it with an explicit
    # environment and publishes its branch from the operator side. A run persisted before trust
    # modes keeps the ambient launch and worker-pushes contract it was bound under.
    launches_worker = node_id == "implement" and not request.runner.legacy
    return FixtureDispatch(
        run_id=str(request.run.id),
        node_id=node_id,
        attempt_id=attempt_id,
        repository_id=request.repository_id,
        repository_path=request.repository_path,
        workspace_path=request.workspace_path,
        base_revision=request.run.base_revision if base_revision is None else base_revision,
        command=command,
        expected_attempt_version=0,
        operation_id=operation_id,
        dependencies=dependencies,
        workflow_definition_id=request.manifest.id.value,
        retain_workspace=node_id == "implement",
        log_directory=(
            request.execution_configuration.artifact_root / "worker-logs"
            if node_id == "implement"
            else None
        ),
        workspace_mode=(
            WORKSPACE_MODE_TASK_LOCAL_CLONE
            if request.runner.isolated_mode
            else WORKSPACE_MODE_GIT_WORKTREE
        ),
        environment_keys=(
            None
            if not launches_worker
            else (LAUNCHER_KEYS if request.runner.isolated_mode else TRUSTED_LOCAL_WORKER_KEYS)
        ),
        branch_publication=(
            BRANCH_PUBLICATION_OPERATOR if launches_worker else BRANCH_PUBLICATION_WORKER
        ),
    )


def _require_bound_selection(request: Stage1RunRequest) -> None:
    """Fail loudly unless a new run binds an explicit execution selection."""
    if request.execution_selection.legacy:
        raise InvalidInputError(
            "a new Stage 1 run must bind an execution selection; rebuild the request with "
            "the current `enginery stage1 build-request`",
            details={"run_id": str(request.run.id)},
        )


def _selection_digest(request: Stage1RunRequest) -> Digest | None:
    """Return the digest decisions and evidence bind to, or None for a pre-selection run."""
    return None if request.execution_selection.legacy else request.execution_selection.digest


def _require_bound_runner(request: Stage1RunRequest) -> None:
    """Fail loudly unless a new run binds an explicit trust mode.

    Trusted-local is a declared, weaker choice, never what a run gets by omission.
    """
    if request.runner.legacy:
        raise InvalidInputError(
            "a new Stage 1 run must bind a trust mode (--runner trusted-local or isolated-oci); "
            "rebuild the request with the current `enginery stage1 build-request`",
            details={"run_id": str(request.run.id)},
        )


DEFAULT_VALIDATION_COMMAND: tuple[str, ...] = ("uv", "run", "pytest", "-q")
"""The validation command a trusted-local request gets when it names none."""


def require_explicit_isolated_validation(request: Stage1RunRequest) -> None:
    """Refuse an isolated request that validates with the host default instead of naming commands.

    Isolated validation runs offline inside the pinned runner image, which carries python3, node,
    git and uv but none of a project's own test dependencies, so the host default
    (``uv run pytest -q``, which needs to resolve pytest) cannot run there. An isolated run must
    name validation commands that run with what the image has, never inherit one that cannot.
    """
    if request.runner.isolated_mode and request.validation_commands == (
        DEFAULT_VALIDATION_COMMAND,
    ):
        raise InvalidInputError(
            "an isolated run must name its validation commands (--validation-command): isolated "
            "validation runs offline in the pinned runner image, which has python3, node, git "
            "and uv but not the project's test dependencies, so the host default "
            f"`{' '.join(DEFAULT_VALIDATION_COMMAND)}` cannot run there",
            details={"run_id": str(request.run.id)},
        )


def _require_no_opaque_budget(request: Stage1RunRequest) -> None:
    """Refuse a hard-dollar budget that no supported harness enforces.

    A budget that nothing enforces is not a limit, and carrying the number would read as one.
    A run persisted before this rule keeps decoding and running with the value it recorded.
    """
    if request.runner.legacy or request.implementation.cost_budget is None:
        return
    raise InvalidInputError(
        "no supported harness enforces a hard-dollar budget; none can be bound "
        "(the time budget is enforced)",
        details={
            "run_id": str(request.run.id),
            "cost_budget": str(request.implementation.cost_budget),
        },
    )


def _runner_from_state(state: Mapping[str, object]) -> RunnerSelection:
    """Decode the bound trust mode; a run persisted before trust modes decodes as legacy."""
    raw = state.get("runner")
    if raw is None:
        return LEGACY_RUNNER_SELECTION
    return RunnerSelection.from_state(raw)


def _selection_from_state(
    state: Mapping[str, object], *, harness_provider: str
) -> ExecutionSelection:
    raw = state.get("execution_selection")
    if raw is None:
        return ExecutionSelection.legacy_host_default(harness_provider)
    return ExecutionSelection.from_state(raw)


def _require_current_check_contracts(request: Stage1RunRequest) -> None:
    """Fail loudly unless a new run binds the current check contracts."""
    manifest = request.manifest
    if manifest.schema_version < CHECK_BINDING_SCHEMA_VERSION:
        raise InvalidInputError(
            "a new Stage 1 run must bind check contracts: its manifest declares "
            f"schema_version {manifest.schema_version}, below {CHECK_BINDING_SCHEMA_VERSION}; "
            "rebuild the request with the current `enginery stage1 build-request`",
            details={"run_id": str(request.run.id)},
        )
    load_acceptance_baseline().require_manifest_checks(manifest)
    for command in request.validation_commands:
        reject_shell_invocation(command)


def stage1_request_from_state(state: object) -> Stage1RunRequest:
    """Decode a complete persisted-start request from its JSON-compatible state."""
    return _run_from_state(state, aggregate_version=0).request


def _run_from_state(state: object, *, aggregate_version: int) -> Stage1Run:
    if not isinstance(state, dict):
        raise InvalidInputError("Stage 1 run projection must be a mapping")
    run = run_from_dict(_mapping(state, "run"))
    work_item = work_item_from_dict(_mapping(state, "work_item"))
    source_revision = _string(state, "source_revision")
    classification_provenance = _classification_from_state(state)
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
        work_snapshot=WorkLedgerSnapshot(
            work_item=work_item,
            source_revision=source_revision,
            classification_provenance=classification_provenance,
        ),
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
        execution_selection=_selection_from_state(
            state, harness_provider=execution_configuration.harness_provider
        ),
        required_checks=tuple(required_checks_value),
        repair_limit=_integer(state, "repair_limit"),
        implementation=implementation,
        runner=_runner_from_state(state),
        protected_paths=_protected_paths_from_state(state),
        behavioral_checks=(
            None
            if "behavioral_checks" not in state
            else checks_from_state(state["behavioral_checks"])
        ),
    )
    if str(run.id) != _string(state, "run_id"):
        raise InvalidInputError("Stage 1 run projection has a mismatched run_id")
    if status is not run.state:
        raise InvalidInputError("Stage 1 run projection status must match its run state")
    if state.get("request_digest") != str(request.digest):
        raise InvalidInputError("Stage 1 run projection has a mismatched request digest")
    return Stage1Run(request=request, status=status, aggregate_version=aggregate_version)


def _protected_paths_from_state(state: Mapping[str, object]) -> tuple[str, ...]:
    raw = state.get("protected_paths")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(pattern, str) for pattern in raw):
        raise InvalidInputError("Stage 1 protected_paths must be a list of strings")
    return tuple(raw)


def _classification_from_state(
    state: Mapping[str, object],
) -> DeclaredWorkClassification | None:
    raw = state.get("classification_provenance")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidInputError("Stage 1 classification_provenance must be a mapping or null")
    source_provider = _string(raw, "source_provider")
    source_url = _string(raw, "source_url")
    labels = raw.get("canonical_labels")
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise InvalidInputError(
            "Stage 1 classification_provenance canonical_labels must be a list of strings"
        )
    return DeclaredWorkClassification(
        source_provider=source_provider,
        source_url=source_url,
        canonical_labels=tuple(labels),
    )


def _mapping(state: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = state.get(field_name)
    if not isinstance(value, dict):
        raise InvalidInputError(f"Stage 1 run projection {field_name} must be a mapping")
    return value


def _string(state: Mapping[str, object], field_name: str) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"Stage 1 run projection {field_name} must be a non-blank string")
    return value


def _digest_from_string(value: str) -> Digest:
    algorithm, separator, hex_value = value.partition(":")
    if not separator:
        raise InvalidInputError("Stage 1 artifact reference must be an algorithm:hex digest")
    return Digest(algorithm=algorithm, hex_value=hex_value)


def _digest_tuple(state: Mapping[str, object], field_name: str) -> tuple[Digest, ...]:
    values = state.get(field_name)
    if not isinstance(values, list) or not values:
        raise MissingPrerequisiteError(
            f"Stage 1 merge-ready verification requires {field_name}",
            details={"field": field_name},
        )
    if any(not isinstance(value, str) for value in values):
        raise InternalInvariantViolationError(
            "Stage 1 implementation artifacts have an invalid type",
            details={"field": field_name},
        )
    return tuple(_digest_from_string(value) for value in values)


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
        harness_provider=_string(state, "harness_provider"),
        harness_credential_reference=_string(state, "harness_credential_reference"),
        harness_executable=_string(state, "harness_executable"),
        artifact_root=Path(_string(state, "artifact_root")),
    )


def _integer(state: Mapping[str, object], field_name: str) -> int:
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
    "implementation_attempt_id",
    "stage1_request_from_state",
]
