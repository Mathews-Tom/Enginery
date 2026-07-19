from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterEventKind,
    AdapterFingerprint,
    AdapterStatus,
    NormalizedAdapterEvent,
    ProviderKind,
)
from enginery.application.work_ports import (
    HarnessOutput,
    HarnessResult,
    HarnessSession,
    HarnessTask,
)
from enginery.domain.artifact import RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, InvalidInputError, WorkerFailureError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.workflows.implementation import Stage1ImplementationExecutor


@dataclass
class RecordedHarness:
    availability: AdapterAvailability = AdapterAvailability.AVAILABLE
    output_count: int = 1
    event_operation_id: OperationId | None = None

    def __post_init__(self) -> None:
        self.started: list[HarnessTask] = []

    def probe(self) -> AdapterStatus:
        fingerprint = (
            AdapterFingerprint("test-harness", "1", ADAPTER_API_VERSION)
            if self.availability is AdapterAvailability.AVAILABLE
            else None
        )
        return AdapterStatus(ProviderKind.HARNESS, self.availability, fingerprint, "test harness")

    def start(self, task: HarnessTask) -> HarnessSession:
        self.started.append(task)
        return HarnessSession("session-1", task.operation_id)

    def events(self, session: HarnessSession) -> Iterator[NormalizedAdapterEvent]:
        return iter(
            (
                NormalizedAdapterEvent(
                    kind=AdapterEventKind.TERMINAL,
                    occurred_at=datetime.now(UTC),
                    operation_id=self.event_operation_id or session.operation_id,
                    summary="completed",
                ),
            )
        )

    def result(self, session: HarnessSession) -> HarnessResult:
        outputs = tuple(
            HarnessOutput(Digest.of_bytes(str(index).encode()), RedactionClassification.SENSITIVE)
            for index in range(self.output_count)
        )
        return HarnessResult(session.session_id, "succeeded", outputs)

    def cancel(self, session: HarnessSession, *, operation_id: OperationId) -> ReconciliationResult:
        raise AssertionError("cancel is not expected")

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        raise AssertionError("reconcile is not expected")


def _task(tmp_path: Path, *, node_id: str = "low_implement") -> HarnessTask:
    return HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId(node_id),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId("operation-1"),
        workspace_path=tmp_path,
        objective="Implement the approved issue change.",
        acceptance_criteria=("the focused test passes",),
        constraints=("keep the source revision bound",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("redacted OMP output",),
        time_budget_seconds=60,
        cost_budget=Decimal("1"),
    )


def test_executor_runs_one_implementation_task_through_available_harness(tmp_path: Path) -> None:
    harness = RecordedHarness()
    task = _task(tmp_path)

    execution = Stage1ImplementationExecutor(harness).execute(task)

    assert harness.started == [task]
    assert execution.task is task
    assert execution.result.terminal_status == "succeeded"
    assert execution.events[0].operation_id == task.operation_id


def test_executor_refuses_unavailable_harness_before_start(tmp_path: Path) -> None:
    harness = RecordedHarness(availability=AdapterAvailability.UNAVAILABLE)

    with pytest.raises(ExternalConflictError, match="unavailable"):
        Stage1ImplementationExecutor(harness).execute(_task(tmp_path))

    assert harness.started == []


def test_executor_rejects_non_implementation_node_before_start(tmp_path: Path) -> None:
    harness = RecordedHarness()

    with pytest.raises(InvalidInputError, match="implement workflow node"):
        Stage1ImplementationExecutor(harness).execute(_task(tmp_path, node_id="low_validate"))

    assert harness.started == []


def test_executor_rejects_cross_operation_events(tmp_path: Path) -> None:
    harness = RecordedHarness(event_operation_id=OperationId("other-operation"))

    with pytest.raises(InvalidInputError, match="event operation"):
        Stage1ImplementationExecutor(harness).execute(_task(tmp_path))


def test_executor_requires_artifact_backed_harness_output(tmp_path: Path) -> None:
    harness = RecordedHarness(output_count=0)

    with pytest.raises(WorkerFailureError, match="no output artifact"):
        Stage1ImplementationExecutor(harness).execute(_task(tmp_path))
