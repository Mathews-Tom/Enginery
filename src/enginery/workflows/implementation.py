"""Manifest-bound supervised-harness execution composed through ``CoordinatorRuntime``."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from enginery.application.adapter_types import NormalizedAdapterEvent
from enginery.application.work_ports import HarnessResult, HarnessTask
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.execution_receipt import ExecutionReceipt, evaluate_effective_requirement
from enginery.domain.execution_selection import ExecutionHold, HoldReason
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import (
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
    WorkflowDispatch,
)
from enginery.ledger.artifact_store import ArtifactStore


class SupervisedHarness(Protocol):
    """A harness adapter capable of coordinator-supervised execution.

    Structural, not a named provider: any adapter exposing these three
    members satisfies this without a provider-specific import here.
    """

    artifact_store: ArtifactStore

    def supervised_command(self, task: HarnessTask, *, result_path: Path) -> tuple[str, ...]: ...
    def installed_host(self) -> InstalledHost | None: ...

    def collect_supervised(
        self, task: HarnessTask, *, result_path: Path
    ) -> tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]: ...


@dataclass(frozen=True, slots=True)
class Stage1ImplementationExecutor:
    """Prepare and collect one supervised implementation node, without owning scheduling."""

    runtime: CoordinatorRuntime
    harness: SupervisedHarness
    manifest: WorkflowManifest
    head_branch: str | None = None

    def dispatch(self, request: FixtureDispatch, task: HarnessTask) -> WorkflowDispatch:
        """Bind one implementation task to the existing fenced runtime dispatch path."""
        _require_matching_request(request, task)
        command = self.harness.supervised_command(task, result_path=_result_path(task))
        return WorkflowDispatch(request=replace(request, command=command), manifest=self.manifest)

    def collect(
        self,
        *,
        dispatched: DispatchedFixture,
        task: HarnessTask,
        now: datetime,
        result_path: Path | None = None,
        launched_command: tuple[str, ...] | None = None,
    ) -> HarnessResult:
        """Ingest a completed supervised implementation result through the coordinator."""
        _require_matching_dispatch(dispatched, task)
        path = _result_path(task) if result_path is None else result_path
        _, result = self.harness.collect_supervised(task, result_path=path)
        receipt = result.execution_receipt
        hold = _execution_hold(task, receipt, launched_command)
        terminal_result = (
            "passed" if result.terminal_status == "succeeded" and hold is None else "failed"
        )
        result_details: dict[str, object] = {"harness_status": result.terminal_status}
        if receipt is not None:
            result_details["execution_receipt"] = receipt.to_state()
        if not task.selection.legacy:
            result_details["execution_selection_digest"] = str(task.selection.digest)
        if hold is not None:
            result_details["execution_hold"] = hold.to_state()
        if terminal_result == "passed" and self.head_branch is not None:
            try:
                result_details["head_revision"] = self.runtime.verify_implementation_branch(
                    run_id=dispatched.lease.run_id, head_branch=self.head_branch
                )
            except ExternalConflictError as error:
                terminal_result = "failed"
                result_details["branch_verification_error"] = str(error)
        self.runtime.ingest_result(
            envelope=WorkerResultEnvelope(
                run_id=dispatched.lease.run_id,
                node_id=dispatched.lease.node_id,
                attempt_id=dispatched.lease.attempt_id,
                epoch=dispatched.lease.epoch,
                fencing_token=dispatched.lease.fencing_token,
                operation_id=dispatched.lease.operation_id,
                terminal_result=terminal_result,
                artifact_references=tuple(str(output.digest) for output in result.outputs),
                result=result_details,
            ),
            now=now,
        )
        return result


def _result_path(task: HarnessTask) -> Path:
    return (
        task.workspace_path / ".enginery" / "implementation-results" / f"{task.operation_id}.json"
    )


def _contains_sequence(command: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    if not sequence:
        return True
    width = len(sequence)
    return any(command[start : start + width] == sequence for start in range(len(command)))


def _execution_hold(
    task: HarnessTask, receipt: ExecutionReceipt | None, launched_command: tuple[str, ...] | None
) -> ExecutionHold | None:
    """Hold when the receipt misstates what was launched or a strict requirement is unmet."""
    if (
        receipt is not None
        and launched_command is not None
        and not _contains_sequence(launched_command, receipt.submitted_arguments)
    ):
        return ExecutionHold(
            reason=HoldReason.SUBMITTED_CONTROLS_MISMATCH,
            message=(
                "the receipt's submitted controls are not in the command that was launched; "
                "the run is held, not passed"
            ),
            details={"submitted_arguments": " ".join(receipt.submitted_arguments)},
        )
    return evaluate_effective_requirement(task.selection, receipt)


def _require_matching_request(request: FixtureDispatch, task: HarnessTask) -> None:
    if (
        request.run_id != str(task.run_id)
        or request.node_id != str(task.node_id)
        or request.attempt_id != str(task.attempt_id)
        or request.operation_id != str(task.operation_id)
        or request.workspace_path != task.workspace_path
    ):
        raise InvalidInputError("implementation task does not match the runtime dispatch identity")


def _require_matching_dispatch(dispatched: DispatchedFixture, task: HarnessTask) -> None:
    if (
        dispatched.lease.run_id != str(task.run_id)
        or dispatched.lease.node_id != str(task.node_id)
        or dispatched.lease.attempt_id != str(task.attempt_id)
        or dispatched.lease.operation_id != str(task.operation_id)
        or dispatched.workspace.workspace_path != task.workspace_path
    ):
        raise InvalidInputError(
            "implementation task does not match the dispatched runtime identity"
        )


__all__ = ["Stage1ImplementationExecutor"]
