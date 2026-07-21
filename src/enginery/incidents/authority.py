"""Deployment and rollback authority: policy-gated, short-lived broker
grants and their durable evidence record.

Mirrors design's broker pattern (credentials and trust boundaries,
Section 11): a short-lived grant is issued only after the coordinator
records the policy-approved action digest, and the grant, its
issuance/expiry, and the resulting broker outcome are all recorded as
evidence -- never a bare "it worked" claim. Deployment and rollback are
always two independently evaluated policy actions
(``DEPLOYMENT_EXECUTE``, ``DEPLOYMENT_ROLLBACK``); one approval can
never satisfy the other, because ``ApprovalRegistry`` keys every
approval on its exact action-bound schema digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from enginery.domain.errors import InvalidInputError
from enginery.domain.policy_decision import PolicyAction

DEFAULT_GRANT_TTL = timedelta(minutes=5)
_DEPLOYMENT_ACTIONS = frozenset({PolicyAction.DEPLOYMENT_EXECUTE, PolicyAction.DEPLOYMENT_ROLLBACK})


class DeploymentGrantExpiredError(InvalidInputError):
    """Raised when a grant expires before its broker action executes."""


@dataclass(frozen=True, slots=True)
class DeploymentGrant:
    """A short-lived authorization to perform exactly one deployment-broker action."""

    grant_id: str
    action: PolicyAction
    target: str
    principal_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.action not in _DEPLOYMENT_ACTIONS:
            raise InvalidInputError(
                "deployment grant action must be deployment.execute or deployment.rollback",
                details={"action": self.action.value},
            )
        if not self.target.strip():
            raise InvalidInputError("deployment grant target must be non-blank")
        if not self.principal_id.strip():
            raise InvalidInputError("deployment grant principal_id must be non-blank")
        if self.expires_at <= self.issued_at:
            raise InvalidInputError("deployment grant expires_at must be after issued_at")

    def require_not_expired(self, *, reference_time: datetime) -> None:
        if reference_time >= self.expires_at:
            raise DeploymentGrantExpiredError(
                "deployment grant expired before its broker action executed",
                details={
                    "grant_id": self.grant_id,
                    "expires_at": self.expires_at.isoformat(),
                    "reference_time": reference_time.isoformat(),
                },
            )


def issue_grant(
    *,
    action: PolicyAction,
    target: str,
    principal_id: str,
    issued_at: datetime,
    ttl: timedelta = DEFAULT_GRANT_TTL,
) -> DeploymentGrant:
    """Issue a short-lived grant for exactly one deployment-broker action."""
    return DeploymentGrant(
        grant_id=str(uuid4()),
        action=action,
        target=target,
        principal_id=principal_id,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
    )


@dataclass(frozen=True, slots=True)
class DeploymentAuthorityRecord:
    """Durable evidence for one authorized deployment-broker action."""

    incident_id: str
    grant: DeploymentGrant
    policy_decision_id: str
    outcome: str
    detail: str

    def __post_init__(self) -> None:
        if not self.incident_id.strip():
            raise InvalidInputError("authority record incident_id must be non-blank")
        if not self.policy_decision_id.strip():
            raise InvalidInputError("authority record policy_decision_id must be non-blank")
        if not self.detail.strip():
            raise InvalidInputError("authority record detail must be non-blank")


__all__ = [
    "DEFAULT_GRANT_TTL",
    "DeploymentAuthorityRecord",
    "DeploymentGrant",
    "DeploymentGrantExpiredError",
    "issue_grant",
]
