"""Bridge a resolved capability lock to the existing capability.materialize action.

Capability resolution and provenance classification stay free of policy
machinery, and ``enginery.policy`` stays free of capability-specific
knowledge; this module is the one place that translates between them,
reusing the ``capability.materialize`` hard rule and approval flow M4/M6
already established rather than adding a second authority path.
"""

from __future__ import annotations

from enginery.capabilities.lock import (
    ApprovedCapabilities,
    CapabilityApproval,
    CapabilityLock,
    LockedCapability,
)
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema


def materialize_approval_schema(
    entry: LockedCapability, lock: CapabilityLock, *, requesting_principal_id: str
) -> ApprovalSchema:
    """The exact-digest-bound approval request for one pending lock entry.

    Binding both ``capability_locks`` (the whole lock's digest) and
    ``diff_or_artifact_digest`` (this entry's own digest) means any later
    change to either the entry or the rest of the lock supersedes a prior
    approval, matching "any change to a bound field supersedes the
    approval."
    """

    if not entry.requires_human_approval():
        raise ValueError("only a pending lock entry needs an approval schema")
    return ApprovalSchema(
        action=PolicyAction.CAPABILITY_MATERIALIZE,
        requested_capability=f"{entry.name}@{entry.version}",
        capability_locks=str(lock.digest()),
        diff_or_artifact_digest=str(entry.digest),
        capability_introduced_by_run=entry.introduced_by_run,
        requesting_principal_id=requesting_principal_id,
    )


def approved_capabilities(
    lock: CapabilityLock, evaluator: PolicyEvaluator, *, requesting_principal_id: str
) -> ApprovedCapabilities:
    """The digest-bound approvals the current policy state allows for ``lock``.

    A reviewed-base entry never needs a schema. A pending entry needs a
    current, digest-bound human approval already recorded on the
    evaluator's approval registry; this function only evaluates policy for
    the already-locked digests -- it never records or requests an approval,
    so run-introduced capabilities cannot approve themselves through this
    path. Each result names the entry, its closure digest, and this lock's
    digest, so it authorizes only this exact lock: a drifted lock needs
    approvals computed for it.
    """

    lock_digest = lock.digest()
    approvals: set[CapabilityApproval] = set()
    for entry in lock.pending_approval():
        schema = materialize_approval_schema(
            entry, lock, requesting_principal_id=requesting_principal_id
        )
        decision = evaluator.evaluate(schema)
        if decision.result is PolicyResult.ALLOW:
            approvals.add(CapabilityApproval(entry.name, entry.digest, lock_digest))
    return ApprovedCapabilities(frozenset(approvals))


__all__ = ["approved_capabilities", "materialize_approval_schema"]
