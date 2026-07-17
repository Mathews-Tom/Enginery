"""``Run``: one workflow instance bound to a work item (03_SYSTEM_DESIGN.md §9.3).

Declares the aggregate and its fourteen-state lifecycle vocabulary (§10.2).
Guarded transition enforcement lands in a later slice of this stack
alongside the shared transition-table machinery.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.immutable import freeze_mapping


class RunState(enum.Enum):
    """The fourteen run lifecycle states (§10.2)."""

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


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{field_name} must be a non-blank string", details={"field": field_name}
        )


__all__ = ["Run", "RunState"]
