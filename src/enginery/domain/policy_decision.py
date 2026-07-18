"""``PolicyDecision``: a durable policy evaluation record.

This module only encodes the closed action namespace and the decision
record shape. Policy *evaluation* — matching rules, granting authority, and
enforcing the non-overridable hard-rule set — is out of scope for M2.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PolicyDecisionId
from enginery.domain.immutable import freeze_json_mapping, freeze_mapping, thaw_json_value
from enginery.domain.principal import AuthorityPrincipal, PrincipalType


class PolicyAction(enum.Enum):
    """The closed initial action namespace. An action outside this set
    cannot be represented — matching the hard rule that unknown actions are
    denied rather than silently permitted."""

    WORKSPACE_CREATE = "workspace.create"
    AGENT_EXECUTE = "agent.execute"
    CREDENTIAL_GRANT = "credential.grant"
    NETWORK_REQUEST = "network.request"
    CAPABILITY_MATERIALIZE = "capability.materialize"
    EVIDENCE_NON_APPLICABILITY_ACCEPT = "evidence.non_applicability.accept"
    REVIEW_FINDING_WAIVE = "review_finding.waive"
    PULL_REQUEST_OPEN = "pull_request.open"
    PULL_REQUEST_MERGE = "pull_request.merge"
    RELEASE_PREPARE = "release.prepare"
    RELEASE_PUBLISH = "release.publish"
    DEPLOYMENT_EXECUTE = "deployment.execute"
    DEPLOYMENT_ROLLBACK = "deployment.rollback"
    FACTORY_CHANGE_PROPOSE = "factory_change.propose"
    FACTORY_CHANGE_CANARY = "factory_change.canary"
    FACTORY_CHANGE_PROMOTE = "factory_change.promote"
    POLICY_OVERRIDE = "policy.override"


class PolicyResult(enum.Enum):
    """The three policy evaluation results."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN = "require_human"


@dataclass(frozen=True, slots=True)
class ApprovalAttestation:
    """A digest-bound, provenance-carrying approval fact."""

    action: PolicyAction
    schema_digest: Digest
    normalized_inputs: Mapping[str, object]
    approvers: tuple[AuthorityPrincipal, ...]
    approved: bool
    expires_at: datetime | None = None
    superseded: bool = False

    def __post_init__(self) -> None:
        if not self.approvers:
            raise InvalidInputError("approval attestation requires at least one approver")
        if self.normalized_inputs.get("action") != self.action.value:
            raise InvalidInputError("approval attestation action does not match normalized inputs")
        if Digest.of_json(thaw_json_value(self.normalized_inputs)) != self.schema_digest:
            raise InvalidInputError("approval attestation inputs do not match schema digest")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise InvalidInputError("approval attestation expiry must be timezone-aware")
        freeze_json_mapping(self, "normalized_inputs", self.normalized_inputs)

    def is_current(self, reference_time: datetime) -> bool:
        if reference_time.tzinfo is None:
            raise InvalidInputError("approval reference_time must be timezone-aware")
        return (
            self.approved
            and not self.superseded
            and (self.expires_at is None or self.expires_at >= reference_time)
        )

    def binds_input(self, field_name: str, value: object) -> bool:
        """Return whether this attestation binds the exact claimed input."""

        return thaw_json_value(self.normalized_inputs.get(field_name)) == thaw_json_value(value)

    def has_independent_human_approval(self) -> bool:
        """Return whether current provenance proves human producer separation."""

        requester = self.normalized_inputs.get("requesting_principal_id")
        producer_ids = self.normalized_inputs.get("producer_principal_ids")
        if not isinstance(requester, str) or not requester:
            return False
        if producer_ids is not None and (
            not isinstance(producer_ids, (list, tuple))
            or any(not isinstance(producer_id, str) for producer_id in producer_ids)
        ):
            return False
        excluded_ids = {requester, *(producer_ids or [])}
        return all(
            approver.principal_type is PrincipalType.HUMAN and approver.id not in excluded_ids
            for approver in self.approvers
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A durable record of one policy evaluation for one requested action."""

    id: PolicyDecisionId
    action: PolicyAction
    normalized_inputs: Mapping[str, object]
    policy_rule_id: str
    policy_version: str
    result: PolicyResult
    rationale: str
    input_digest: Digest
    decided_at: datetime
    required_evidence: tuple[str, ...] = field(default_factory=tuple)
    required_approver: str | None = None
    superseded: bool = False
    superseded_by: PolicyDecisionId | None = None

    def __post_init__(self) -> None:
        if not self.policy_rule_id.strip():
            raise InvalidInputError("policy_rule_id must be a non-blank string")
        if not self.policy_version.strip():
            raise InvalidInputError("policy_version must be a non-blank string")
        if not self.rationale.strip():
            raise InvalidInputError("rationale must be a non-blank string")
        if self.decided_at.tzinfo is None:
            raise InvalidInputError("decided_at must be a timezone-aware datetime")
        if self.superseded_by is not None and not self.superseded:
            raise InvalidInputError(
                "superseded_by can only be set when superseded is True",
                details={"superseded_by": str(self.superseded_by)},
            )
        freeze_mapping(self, "normalized_inputs", self.normalized_inputs)


__all__ = ["ApprovalAttestation", "PolicyAction", "PolicyDecision", "PolicyResult"]
