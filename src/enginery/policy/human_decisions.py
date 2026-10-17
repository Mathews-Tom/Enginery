"""Adapt authenticated packet decisions to the existing approval policy engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from enginery.application.human_decisions import (
    ActionMismatchError,
    AuthenticatedDecisionContext,
    HumanDecision,
    SubjectMismatchError,
    authenticate_context,
    policy_action,
    subject_digest,
)
from enginery.domain.principal import AuthorityPrincipal
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.schemas import ApprovalSchema


def decision_verifier(
    *,
    run_id: str,
    action: str,
    subject_fields: Mapping[str, object],
    context: AuthenticatedDecisionContext,
    registered_humans: tuple[AuthorityPrincipal, ...],
    schema: ApprovalSchema,
    reason: str = "",
) -> Callable[[datetime, datetime], HumanDecision]:
    """Return verification invoked under the ledger's nonce-consumption lock.

    A fresh registry is deliberately scoped to this transaction: a failed durable
    write cannot leave usable in-memory authority behind. The registry still owns
    every hard rule, producer separation, canonical digest, and approval record.
    """
    digest = subject_digest(run_id, subject_fields)

    def verify(decided_at: datetime, expires_at: datetime) -> HumanDecision:
        authenticate_context(context, registered_humans)
        if schema.action != policy_action(action):
            raise ActionMismatchError("the approval schema does not bind the requested action")
        if schema.work_snapshot != digest or schema.target_resource != run_id:
            raise SubjectMismatchError("the approval schema does not bind the current packet")
        if schema.override_expires_at is not None and schema.override_expires_at < expires_at:
            expiry = schema.override_expires_at
        else:
            expiry = expires_at
        record = ApprovalRegistry(registered_humans).record_approval(
            schema,
            (context.principal,),
            request_id=run_id,
            expires_at=expiry,
            decided_at=decided_at,
        )
        return HumanDecision(
            id=record.id,
            run_id=run_id,
            principal=context.principal,
            action=action,
            subject_digest=digest,
            subject_fields=subject_fields,
            os_uid=context.os_uid,
            tty_name=context.tty_name,
            decided_at=record.decided_at,
            expires_at=expiry,
            schema_digest=record.schema_digest,
            normalized_inputs=record.normalized_inputs,
            reason=reason,
        )

    return verify
