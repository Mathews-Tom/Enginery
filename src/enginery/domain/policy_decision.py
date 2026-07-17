"""``PolicyDecision``: a durable policy evaluation record (03_SYSTEM_DESIGN.md §9.6, §15).

This module only encodes the closed action namespace and the decision
record shape. Policy *evaluation* — matching rules, granting authority, and
enforcing the non-overridable hard-rule set — is out of scope for M2 (see
`.docs/DEVELOPMENT_PLAN.md` M4).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PolicyDecisionId
from enginery.domain.immutable import freeze_mapping


class PolicyAction(enum.Enum):
    """The closed initial action namespace (§15). An action outside this set
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
    """The three policy evaluation results (§15)."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN = "require_human"


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


__all__ = ["PolicyAction", "PolicyDecision", "PolicyResult"]
