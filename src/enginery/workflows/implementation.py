"""Manifest-bound OMP execution composed through ``CoordinatorRuntime``."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from enginery.adapters.omp import OmpHarness
from enginery.application.work_ports import HarnessResult, HarnessTask
from enginery.domain.errors import InvalidInputError
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.engine.results import WorkerResultEnvelope
from enginery.engine.runtime import (
    CoordinatorRuntime,
    DispatchedFixture,
    FixtureDispatch,
    WorkflowDispatch,
)


@dataclass(frozen=True, slots=True)
class Stage1ImplementationExecutor:
    """Prepare and collect one OMP node without owning scheduling or processes."""

    runtime: CoordinatorRuntime
    harness: OmpHarness
    manifest: WorkflowManifest

    def dispatch(self, request: FixtureDispatch, task: HarnessTask) -> WorkflowDispatch:
        """Bind one OMP task to the existing fenced runtime dispatch path."""
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
    ) -> HarnessResult:
        """Ingest a completed supervised OMP result through the coordinator."""
        _require_matching_dispatch(dispatched, task)
        path = _result_path(task) if result_path is None else result_path
        _, result = self.harness.collect_supervised(task, result_path=path)
        terminal_result = "passed" if result.terminal_status == "succeeded" else "failed"
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
                result={"harness_status": result.terminal_status},
            ),
            now=now,
        )
        return result


def _result_path(task: HarnessTask) -> Path:
    return task.workspace_path / ".enginery" / "omp-results" / f"{task.operation_id}.json"


def _require_matching_request(request: FixtureDispatch, task: HarnessTask) -> None:
    if (
        request.run_id != str(task.run_id)
        or request.node_id != str(task.node_id)
        or request.attempt_id != str(task.attempt_id)
        or request.operation_id != str(task.operation_id)
        or request.workspace_path != task.workspace_path
    ):
        raise InvalidInputError("OMP task does not match the runtime dispatch identity")


def _require_matching_dispatch(dispatched: DispatchedFixture, task: HarnessTask) -> None:
    if (
        dispatched.lease.run_id != str(task.run_id)
        or dispatched.lease.node_id != str(task.node_id)
        or dispatched.lease.attempt_id != str(task.attempt_id)
        or dispatched.lease.operation_id != str(task.operation_id)
        or dispatched.workspace.workspace_path != task.workspace_path
    ):
        raise InvalidInputError("OMP task does not match the dispatched runtime identity")


__all__ = ["Stage1ImplementationExecutor"]
