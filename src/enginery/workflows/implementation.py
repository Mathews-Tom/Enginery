"""Bounded Stage 1 implementation-node execution through a harness port."""

from __future__ import annotations

from dataclasses import dataclass

from enginery.application.adapter_types import AdapterAvailability, NormalizedAdapterEvent
from enginery.application.work_ports import HarnessPort, HarnessResult, HarnessSession, HarnessTask
from enginery.domain.errors import ExternalConflictError, InvalidInputError, WorkerFailureError
from enginery.domain.ids import NodeId

_IMPLEMENT_NODE_SUFFIX = "_implement"


@dataclass(frozen=True, slots=True)
class Stage1ImplementationResult:
    """One completed harness invocation bound to an implementation node."""

    task: HarnessTask
    session: HarnessSession
    events: tuple[NormalizedAdapterEvent, ...]
    result: HarnessResult

    def __post_init__(self) -> None:
        if self.session.operation_id != self.task.operation_id:
            raise InvalidInputError("harness session operation does not match implementation task")
        if any(event.operation_id != self.task.operation_id for event in self.events):
            raise InvalidInputError("harness event operation does not match implementation task")
        if self.result.session_id != self.session.session_id:
            raise InvalidInputError("harness result session does not match implementation session")
        if not self.result.outputs:
            raise WorkerFailureError("implementation harness produced no output artifact")


class Stage1ImplementationExecutor:
    """Execute exactly one isolated implementation node through the selected harness."""

    def __init__(self, harness: HarnessPort) -> None:
        self._harness = harness

    def execute(self, task: HarnessTask) -> Stage1ImplementationResult:
        """Probe, invoke, and collect one source-bound implementation task."""
        if not _is_implementation_node(task.node_id):
            raise InvalidInputError("Stage 1 implementation requires an implement workflow node")
        status = self._harness.probe()
        if status.availability is not AdapterAvailability.AVAILABLE:
            raise ExternalConflictError("selected implementation harness is unavailable")
        session = self._harness.start(task)
        events = tuple(self._harness.events(session))
        result = self._harness.result(session)
        return Stage1ImplementationResult(task=task, session=session, events=events, result=result)


def _is_implementation_node(node_id: NodeId) -> bool:
    return node_id.value.endswith(_IMPLEMENT_NODE_SUFFIX)


__all__ = ["Stage1ImplementationExecutor", "Stage1ImplementationResult"]
