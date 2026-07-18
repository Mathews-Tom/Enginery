"""Authenticated approval records and digest-bound supersession."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from enginery.domain.digests import Digest
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.rules import HardRuleEnforcer
from enginery.policy.schemas import ApprovalSchema


class ApprovalOutcome(Enum):
    """The only terminal outcomes available through the approval channel."""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """An authenticated, digest-bound human decision."""

    id: str
    request_id: str | None
    schema_digest: Digest
    action: str
    approvers: tuple[AuthorityPrincipal, ...]
    outcome: ApprovalOutcome
    decided_at: datetime
    expires_at: datetime | None = None
    superseded: bool = False

    def is_current(self, reference_time: datetime) -> bool:
        return (
            self.outcome is ApprovalOutcome.APPROVED
            and not self.superseded
            and (self.expires_at is None or self.expires_at >= reference_time)
        )


class ApprovalRegistry:
    """In-memory approval channel model with explicit registered humans."""

    def __init__(self, registered_humans: Iterable[AuthorityPrincipal] = ()) -> None:
        self._registered_humans: dict[str, AuthorityPrincipal] = {}
        self._approvals_by_digest: dict[str, ApprovalRecord] = {}
        self._latest_by_request: dict[str, ApprovalRecord] = {}
        for principal in registered_humans:
            self.register_human(principal)

    def register_human(self, principal: AuthorityPrincipal) -> None:
        if principal.principal_type is not PrincipalType.HUMAN:
            raise ValueError("only human authority principals can be registered")
        self._registered_humans[principal.id] = principal

    @property
    def registered_human_ids(self) -> frozenset[str]:
        """Return the configured authority principals without exposing mutation."""

        return frozenset(self._registered_humans)

    def record_approval(
        self,
        schema: ApprovalSchema,
        approvers: Iterable[AuthorityPrincipal],
        *,
        request_id: str | None = None,
        expires_at: datetime | None = None,
        decided_at: datetime | None = None,
    ) -> ApprovalRecord:
        """Record one active human approval and supersede its prior request."""

        decision_time = decided_at or datetime.now(UTC)
        if decision_time.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if expires_at is not None and expires_at < decision_time:
            raise ValueError("expires_at cannot precede decided_at")
        approval_tuple = tuple(approvers)
        HardRuleEnforcer.enforce_request(schema)
        HardRuleEnforcer.enforce_approval(
            schema,
            approval_tuple,
            self._registered_humans.keys(),
        )
        record = ApprovalRecord(
            id=str(uuid4()),
            request_id=request_id,
            schema_digest=schema.digest(),
            action=schema.action.value,
            approvers=approval_tuple,
            outcome=ApprovalOutcome.APPROVED,
            decided_at=decision_time,
            expires_at=expires_at,
        )
        self._store(record)
        return record

    def record_rejection(
        self,
        schema: ApprovalSchema,
        approvers: Iterable[AuthorityPrincipal],
        *,
        request_id: str | None = None,
        decided_at: datetime | None = None,
    ) -> ApprovalRecord:
        """Record an authenticated rejection without creating usable authority."""

        decision_time = decided_at or datetime.now(UTC)
        approval_tuple = tuple(approvers)
        HardRuleEnforcer.enforce_approval(
            schema,
            approval_tuple,
            self._registered_humans.keys(),
        )
        record = ApprovalRecord(
            id=str(uuid4()),
            request_id=request_id,
            schema_digest=schema.digest(),
            action=schema.action.value,
            approvers=approval_tuple,
            outcome=ApprovalOutcome.REJECTED,
            decided_at=decision_time,
        )
        self._store(record)
        return record

    def get_approval(
        self,
        schema: ApprovalSchema,
        *,
        reference_time: datetime | None = None,
    ) -> ApprovalRecord | None:
        """Return only a current approval for this exact canonical digest."""

        record = self._approvals_by_digest.get(schema.digest().hex_value)
        current_time = reference_time or datetime.now(UTC)
        if record is None or not record.is_current(current_time):
            return None
        return record

    def _store(self, record: ApprovalRecord) -> None:
        if record.request_id is not None:
            previous = self._latest_by_request.get(record.request_id)
            if previous is not None:
                self._approvals_by_digest[previous.schema_digest.hex_value] = replace(
                    previous,
                    superseded=True,
                )
            self._latest_by_request[record.request_id] = record
        self._approvals_by_digest[record.schema_digest.hex_value] = record


__all__ = ["ApprovalOutcome", "ApprovalRecord", "ApprovalRegistry"]
