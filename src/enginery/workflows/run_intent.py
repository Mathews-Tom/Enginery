"""Stage 1 run request: the immutable data bound before a run progresses, and its state codec.

The request, its execution configuration and implementation attempt, and the durable run
projection are decoded here from their persisted JSON-compatible state. This module owns the
request digest and historical decoding; the run lifecycle in ``run_lifecycle`` consumes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from enginery.application.behavioral_checks import BehavioralCheck, checks_from_state
from enginery.application.branch_publication import ProtectedPathPolicy
from enginery.application.work_ports import DeclaredWorkClassification, WorkLedgerSnapshot
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.ids import OperationId
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
from enginery.domain.workflow.manifest import WorkflowManifest


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
    "Stage1ImplementationRequest",
    "Stage1Run",
    "Stage1RunRequest",
    "stage1_request_from_state",
]
