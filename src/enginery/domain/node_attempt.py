"""``NodeAttempt``: one attempt to execute a workflow node (03_SYSTEM_DESIGN.md §9.4).

Declares the aggregate, its ten-state lifecycle vocabulary (§10.3), the
reconciliation-result vocabulary reused by the shared four-result
reconciliation contract (§7.10, §10.3), the three-value evidence result
(§16.1) a completed attempt carries, and guarded transition enforcement.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass, InvalidInputError
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.domain.state_machine import TransitionTable


class NodeAttemptState(enum.Enum):
    """The ten node-attempt lifecycle states (§10.3)."""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    RECONCILING = "reconciling"
    OUTPUT_PENDING = "output_pending"
    EVIDENCE_PENDING = "evidence_pending"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ReconciliationResult(enum.Enum):
    """The four reconciliation outcomes for an ambiguous side effect (§7.10)."""

    NOT_FOUND = "not_found"
    FOUND_MATCHING = "found_matching"
    FOUND_CONFLICTING = "found_conflicting"
    INDETERMINATE = "indeterminate"


class EvidenceResult(enum.Enum):
    """The three evidence-verification outcomes (§16.1). Only ``PASS`` succeeds."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


NODE_ATTEMPT_TRANSITIONS: TransitionTable[NodeAttemptState] = TransitionTable(
    edges={
        NodeAttemptState.PENDING: frozenset({NodeAttemptState.LEASED, NodeAttemptState.CANCELLED}),
        NodeAttemptState.LEASED: frozenset(
            {
                NodeAttemptState.RUNNING,
                NodeAttemptState.CANCELLED,
                NodeAttemptState.TIMED_OUT,
            }
        ),
        NodeAttemptState.RUNNING: frozenset(
            {
                NodeAttemptState.OUTPUT_PENDING,
                NodeAttemptState.RECONCILING,
                NodeAttemptState.FAILED,
                NodeAttemptState.CANCELLED,
                NodeAttemptState.TIMED_OUT,
            }
        ),
        NodeAttemptState.RECONCILING: frozenset(
            {
                NodeAttemptState.OUTPUT_PENDING,
                NodeAttemptState.FAILED,
                NodeAttemptState.CANCELLED,
                NodeAttemptState.TIMED_OUT,
            }
        ),
        NodeAttemptState.OUTPUT_PENDING: frozenset(
            {
                NodeAttemptState.EVIDENCE_PENDING,
                NodeAttemptState.FAILED,
                NodeAttemptState.CANCELLED,
                NodeAttemptState.TIMED_OUT,
            }
        ),
        NodeAttemptState.EVIDENCE_PENDING: frozenset(
            {
                NodeAttemptState.PASSED,
                NodeAttemptState.FAILED,
                NodeAttemptState.CANCELLED,
                NodeAttemptState.TIMED_OUT,
            }
        ),
    },
    terminal_states=frozenset(
        {
            NodeAttemptState.PASSED,
            NodeAttemptState.FAILED,
            NodeAttemptState.CANCELLED,
            NodeAttemptState.TIMED_OUT,
        }
    ),
)


_RECONCILIATION_OUTCOME_STATE: dict[ReconciliationResult, NodeAttemptState] = {
    ReconciliationResult.FOUND_MATCHING: NodeAttemptState.OUTPUT_PENDING,
    ReconciliationResult.NOT_FOUND: NodeAttemptState.FAILED,
    ReconciliationResult.FOUND_CONFLICTING: NodeAttemptState.FAILED,
    ReconciliationResult.INDETERMINATE: NodeAttemptState.FAILED,
}


@dataclass(frozen=True, slots=True)
class NodeAttempt:
    """One attempt to execute one node. Retries create new attempts."""

    id: NodeAttemptId
    run_id: RunId
    node_id: NodeId
    attempt_number: int
    actor: str
    input_digest: Digest
    state: NodeAttemptState
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    emitted_event_range: tuple[int, int] | None = None
    output_artifact_ids: tuple[ArtifactId, ...] = field(default_factory=tuple)
    evidence_result: EvidenceResult | None = None
    cost_amount: float | None = None
    duration_seconds: float | None = None
    failure_class: FailureClass | None = None
    reconciliation_result: ReconciliationResult | None = None
    schema_version: int = field(default=1)

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise InvalidInputError(
                "attempt_number must be at least 1",
                details={"attempt_number": self.attempt_number},
            )
        if not self.actor.strip():
            raise InvalidInputError("actor must be a non-blank string")
        _require_aware(self.lease_expires_at, field_name="lease_expires_at")
        _require_aware(self.started_at, field_name="started_at")
        _require_aware(self.completed_at, field_name="completed_at")
        if self.emitted_event_range is not None:
            start, end = self.emitted_event_range
            if start < 0 or end < start:
                raise InvalidInputError(
                    "emitted_event_range must satisfy 0 <= start <= end",
                    details={"emitted_event_range": self.emitted_event_range},
                )
        if self.cost_amount is not None and self.cost_amount < 0:
            raise InvalidInputError(
                "cost_amount cannot be negative", details={"cost_amount": self.cost_amount}
            )
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise InvalidInputError(
                "duration_seconds cannot be negative",
                details={"duration_seconds": self.duration_seconds},
            )
        if self.schema_version < 1:
            raise InvalidInputError(
                "schema_version must be at least 1",
                details={"schema_version": self.schema_version},
            )

    def transition_to(self, target: NodeAttemptState) -> NodeAttempt:
        """Return a new ``NodeAttempt`` in ``target`` state, or raise if the
        transition is not legal from the current state (§10.3)."""
        NODE_ATTEMPT_TRANSITIONS.require(self.state, target)
        return replace(self, state=target)

    def resolve_reconciliation(self, result: ReconciliationResult) -> NodeAttempt:
        """Apply a reconciliation outcome from the ``reconciling`` state (§7.10, §10.3).

        ``found_matching`` adopts the observed result and continues to
        ``output_pending``. ``not_found``, ``found_conflicting``, and
        ``indeterminate`` all end the attempt with a classified ``failed``
        result; the run, not this attempt, decides whether to create a new
        attempt under the same operation ID or escalate to a human.
        """
        target = _RECONCILIATION_OUTCOME_STATE[result]
        NODE_ATTEMPT_TRANSITIONS.require(self.state, target)
        return replace(self, state=target, reconciliation_result=result)


def _require_aware(value: datetime | None, *, field_name: str) -> None:
    if value is not None and value.tzinfo is None:
        raise InvalidInputError(
            f"{field_name} must be a timezone-aware datetime", details={"field": field_name}
        )


__all__ = [
    "NODE_ATTEMPT_TRANSITIONS",
    "EvidenceResult",
    "NodeAttempt",
    "NodeAttemptState",
    "ReconciliationResult",
]
