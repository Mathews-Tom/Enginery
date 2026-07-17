"""``Run``: one workflow instance bound to a work item.

Declares the aggregate, its fourteen-state lifecycle vocabulary, and
guarded transition enforcement built on the shared ``TransitionTable``
machinery. Note that ``blocked`` is a *terminal* state for a run — unlike a
work item's recoverable ``blocked`` state — matching the design's explicit
"Terminal states" distinction for run lifecycle transitions.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.immutable import freeze_mapping
from enginery.domain.state_machine import TransitionTable


class RunState(enum.Enum):
    """The fourteen run lifecycle states."""

    CREATED = "created"
    PREFLIGHT = "preflight"
    AWAITING_POLICY = "awaiting_policy"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    RECONCILING = "reconciling"
    EVIDENCE_VERIFICATION = "evidence_verification"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SUPERSEDED = "superseded"


RUN_TRANSITIONS: TransitionTable[RunState] = TransitionTable(
    edges={
        RunState.CREATED: frozenset({RunState.PREFLIGHT}),
        RunState.PREFLIGHT: frozenset(
            {
                RunState.AWAITING_POLICY,
                RunState.QUEUED,
                RunState.BLOCKED,
                RunState.REJECTED,
                RunState.SUPERSEDED,
            }
        ),
        RunState.AWAITING_POLICY: frozenset(
            {RunState.QUEUED, RunState.AWAITING_HUMAN, RunState.REJECTED, RunState.SUPERSEDED}
        ),
        RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLED, RunState.SUPERSEDED}),
        RunState.RUNNING: frozenset(
            {
                RunState.AWAITING_POLICY,
                RunState.AWAITING_HUMAN,
                RunState.RECONCILING,
                RunState.EVIDENCE_VERIFICATION,
                RunState.BLOCKED,
                RunState.CANCELLED,
                RunState.FAILED,
                RunState.SUPERSEDED,
            }
        ),
        RunState.AWAITING_HUMAN: frozenset(
            {
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.RECONCILING,
                RunState.REJECTED,
                RunState.CANCELLED,
                RunState.SUPERSEDED,
            }
        ),
        RunState.RECONCILING: frozenset(
            {
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.EVIDENCE_VERIFICATION,
                RunState.AWAITING_HUMAN,
                RunState.BLOCKED,
                RunState.FAILED,
                RunState.SUPERSEDED,
            }
        ),
        RunState.EVIDENCE_VERIFICATION: frozenset(
            {
                RunState.RUNNING,
                RunState.RECONCILING,
                RunState.AWAITING_HUMAN,
                RunState.SUCCEEDED,
                RunState.BLOCKED,
                RunState.FAILED,
                RunState.SUPERSEDED,
            }
        ),
    },
    terminal_states=frozenset(
        {
            RunState.SUCCEEDED,
            RunState.BLOCKED,
            RunState.REJECTED,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.SUPERSEDED,
        }
    ),
)


@dataclass(frozen=True, slots=True)
class Run:
    """A workflow instance bound to a work item, definition, and lock set."""

    id: RunId
    work_item_id: WorkItemId
    work_item_snapshot_digest: Digest
    workflow_definition_id: WorkflowDefinitionId
    workflow_definition_digest: Digest
    repository: str
    base_revision: str
    policy_set_version: str
    adapter_versions: Mapping[str, str]
    capability_lock_digest: Digest
    environment_manifest_digest: Digest
    configuration_snapshot_digest: Digest
    state: RunState
    aggregate_version: int = field(default=0)

    def __post_init__(self) -> None:
        _require_non_blank(self.repository, field_name="repository")
        _require_non_blank(self.base_revision, field_name="base_revision")
        _require_non_blank(self.policy_set_version, field_name="policy_set_version")
        if self.aggregate_version < 0:
            raise InvalidInputError(
                "aggregate_version cannot be negative",
                details={"aggregate_version": self.aggregate_version},
            )
        for adapter, version in self.adapter_versions.items():
            if not adapter.strip() or not version.strip():
                raise InvalidInputError(
                    "adapter_versions keys and values must be non-blank",
                    details={"adapter": adapter, "version": version},
                )
        freeze_mapping(self, "adapter_versions", self.adapter_versions)

    def transition_to(self, target: RunState) -> Run:
        """Return a new ``Run`` in ``target`` state, or raise if the
        transition is not legal from the current state."""
        RUN_TRANSITIONS.require(self.state, target)
        return replace(self, state=target, aggregate_version=self.aggregate_version + 1)


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


__all__ = ["RUN_TRANSITIONS", "Run", "RunState"]
