"""Authenticated approval records and digest-bound supersession."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from enginery.domain.digests import Digest
from enginery.domain.immutable import freeze_json_mapping, thaw_json_value
from enginery.domain.policy_decision import ApprovalAttestation, PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.rules import HardRuleEnforcer
from enginery.policy.schemas import ApprovalSchema


class ApprovalOutcome(Enum):
    """The only terminal outcomes available through the approval channel."""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """An authenticated, digest-bound authority decision with retained inputs."""

    id: str
    request_id: str | None
    schema_digest: Digest
    action: str
    normalized_inputs: Mapping[str, object]
    approvers: tuple[AuthorityPrincipal, ...]
    outcome: ApprovalOutcome
    decided_at: datetime
    expires_at: datetime | None = None
    superseded: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.action.strip():
            raise ValueError("approval record id and action must be non-blank")
        if self.request_id is not None and not self.request_id.strip():
            raise ValueError("approval request_id must be non-blank when provided")
        if not self.approvers:
            raise ValueError("approval record requires at least one approver")
        if self.decided_at.tzinfo is None:
            raise ValueError("approval decided_at must be timezone-aware")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("approval expires_at must be timezone-aware")
        if self.expires_at is not None and self.expires_at < self.decided_at:
            raise ValueError("approval expires_at cannot precede decided_at")
        if self.normalized_inputs.get("action") != self.action:
            raise ValueError("approval record action does not match normalized inputs")
        if Digest.of_json(thaw_json_value(self.normalized_inputs)) != self.schema_digest:
            raise ValueError("approval record inputs do not match schema digest")
        freeze_json_mapping(self, "normalized_inputs", self.normalized_inputs)

    def is_current(self, reference_time: datetime) -> bool:
        if reference_time.tzinfo is None:
            raise ValueError("reference_time must be timezone-aware")

        return (
            self.outcome is ApprovalOutcome.APPROVED
            and not self.superseded
            and (self.expires_at is None or self.expires_at >= reference_time)
        )

    def attestation(self) -> ApprovalAttestation:
        """Project this audited policy record into a domain approval fact."""

        return ApprovalAttestation(
            action=PolicyAction(self.action),
            schema_digest=self.schema_digest,
            normalized_inputs=self.normalized_inputs,
            approvers=self.approvers,
            approved=self.outcome is ApprovalOutcome.APPROVED,
            expires_at=self.expires_at,
            superseded=self.superseded,
        )


class ApprovalRegistry:
    """In-memory approval channel model with explicit registered humans."""

    def __init__(self, registered_humans: Iterable[AuthorityPrincipal] = ()) -> None:
        self._registered_humans: dict[str, AuthorityPrincipal] = {}
        self._approvals_by_digest: dict[str, ApprovalRecord] = {}
        self._latest_by_request: dict[str, ApprovalRecord] = {}
        self._records_by_id: dict[str, ApprovalRecord] = {}
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

    @property
    def records(self) -> tuple[ApprovalRecord, ...]:
        """Return every recorded decision, including superseded decisions."""

        return tuple(self._records_by_id.values())

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
        normalized_inputs = schema.canonical_inputs()
        record = ApprovalRecord(
            id=str(uuid4()),
            request_id=request_id,
            schema_digest=Digest.of_json(normalized_inputs),
            action=schema.action.value,
            normalized_inputs=normalized_inputs,
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

        if decision_time.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        approval_tuple = tuple(approvers)
        HardRuleEnforcer.enforce_request(schema)
        HardRuleEnforcer.enforce_approval(
            schema,
            approval_tuple,
            self._registered_humans.keys(),
        )
        normalized_inputs = schema.canonical_inputs()
        record = ApprovalRecord(
            id=str(uuid4()),
            request_id=request_id,
            schema_digest=Digest.of_json(normalized_inputs),
            action=schema.action.value,
            normalized_inputs=normalized_inputs,
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
                superseded = replace(previous, superseded=True)
                self._approvals_by_digest[previous.schema_digest.hex_value] = superseded
                self._records_by_id[previous.id] = superseded
            self._latest_by_request[record.request_id] = record
        self._approvals_by_digest[record.schema_digest.hex_value] = record
        self._records_by_id[record.id] = record


__all__ = ["ApprovalOutcome", "ApprovalRecord", "ApprovalRegistry"]
