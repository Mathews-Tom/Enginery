"""``Artifact``: one content-addressed output or evidence item."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId


class ArtifactKind(enum.Enum):
    """Illustrative artifact kinds; not an exhaustive vocabulary."""

    PLAN = "plan"
    PATCH = "patch"
    TRANSCRIPT = "transcript"
    LOG = "log"
    TEST_REPORT = "test_report"
    REVIEW_REPORT = "review_report"
    PR_METADATA = "pr_metadata"
    RELEASE_MANIFEST = "release_manifest"
    EVALUATION_RESULT = "evaluation_result"
    HUMAN_DECISION = "human_decision"


class RedactionClassification(enum.Enum):
    """The sensitivity classification an artifact carries before persistence."""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


@dataclass(frozen=True, slots=True)
class Artifact:
    """A content-addressed output or evidence item produced by one attempt."""

    id: ArtifactId
    digest: Digest
    byte_size: int
    media_type: str
    kind: ArtifactKind
    run_id: RunId
    node_id: NodeId
    attempt_id: NodeAttemptId
    storage_reference: str
    redaction: RedactionClassification
    created_at: datetime
    schema_version: int = field(default=1)

    def __post_init__(self) -> None:
        if self.byte_size < 0:
            raise InvalidInputError(
                "byte_size cannot be negative", details={"byte_size": self.byte_size}
            )
        if not self.media_type.strip():
            raise InvalidInputError("media_type must be a non-blank string")
        if not self.storage_reference.strip():
            raise InvalidInputError("storage_reference must be a non-blank string")
        if self.created_at.tzinfo is None:
            raise InvalidInputError("created_at must be a timezone-aware datetime")
        if self.schema_version < 1:
            raise InvalidInputError(
                "schema_version must be at least 1",
                details={"schema_version": self.schema_version},
            )


__all__ = ["Artifact", "ArtifactKind", "RedactionClassification"]
