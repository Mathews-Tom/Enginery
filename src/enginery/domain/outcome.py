"""``Outcome``: a post-execution observation, distinct from workflow completion
(03_SYSTEM_DESIGN.md §9.8).

Outcomes never rewrite a completed work item. A reopened issue or escaped
defect creates a new linked work item and retains the prior item and
outcome unchanged — enforced here by requiring ``linked_work_item_id``
exactly for those two kinds.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import OutcomeId, RunId, WorkItemId
from enginery.domain.immutable import freeze_mapping


class OutcomeKind(enum.Enum):
    """The observation categories named in §9.8."""

    PR_ACCEPTED = "pr_accepted"
    PR_REJECTED = "pr_rejected"
    PR_ABANDONED = "pr_abandoned"
    MERGE_RESULT = "merge_result"
    CI_STABILITY = "ci_stability"
    RELEASE_RESULT = "release_result"
    DEPLOYMENT_RESULT = "deployment_result"
    ROLLBACK = "rollback"
    REOPENED_ISSUE = "reopened_issue"
    ESCAPED_DEFECT = "escaped_defect"
    USER_RATED_QUALITY = "user_rated_quality"


_KINDS_REQUIRING_LINKED_WORK_ITEM = frozenset(
    {OutcomeKind.REOPENED_ISSUE, OutcomeKind.ESCAPED_DEFECT}
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """An observation after workflow execution, distinct from workflow
    completion. The completed work item and its outcome are never rewritten."""

    id: OutcomeId
    work_item_id: WorkItemId
    kind: OutcomeKind
    observed_at: datetime
    run_id: RunId | None = None
    linked_work_item_id: WorkItemId | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = field(default=1)

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise InvalidInputError("observed_at must be a timezone-aware datetime")
        requires_link = self.kind in _KINDS_REQUIRING_LINKED_WORK_ITEM
        if requires_link and self.linked_work_item_id is None:
            raise InvalidInputError(
                f"{self.kind.value} outcomes must declare linked_work_item_id",
                details={"kind": self.kind.value},
            )
        if not requires_link and self.linked_work_item_id is not None:
            raise InvalidInputError(
                "linked_work_item_id is only valid for reopened_issue and escaped_defect outcomes",
                details={"kind": self.kind.value},
            )
        if self.linked_work_item_id == self.work_item_id:
            raise InvalidInputError(
                "linked_work_item_id must differ from work_item_id",
                details={"work_item_id": str(self.work_item_id)},
            )
        if self.schema_version < 1:
            raise InvalidInputError(
                "schema_version must be at least 1",
                details={"schema_version": self.schema_version},
            )
        freeze_mapping(self, "detail", self.detail)


__all__ = ["Outcome", "OutcomeKind"]
