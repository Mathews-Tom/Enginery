"""Trusted terminal ingress and durable effect checks over the existing run ledger."""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from enginery.application.human_decisions import (
    ActionMismatchError,
    AuthenticatedDecisionContext,
    AuthenticationError,
    HumanDecision,
    PrincipalMismatchError,
    ReviewAuthority,
    StaleSubjectError,
    SubjectMismatchError,
    authenticate_context,
    bounded_json,
)
from enginery.application.review_packet import ReviewPacket
from enginery.cli.config_locations import read_user_layer
from enginery.cli.factory_review import _open, build_packet
from enginery.domain.digests import Digest
from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE
from enginery.evidence.review_rendering import escape_terminal
from enginery.ledger.human_decisions import (
    consume_decision,
    issue_challenge,
    require_current_decision,
)
from enginery.ledger.redaction import assert_no_raw_credentials
from enginery.ledger.service import LedgerService
from enginery.policy.human_decisions import decision_verifier
from enginery.policy.rules import HardRuleEnforcer, HardRuleError
from enginery.policy.schemas import ApprovalSchema
from enginery.workflows.stage1 import Stage1RunRequest

_PUBLICATION = PolicyAction.PULL_REQUEST_OPEN.value
_OVERRIDE = PolicyAction.POLICY_OVERRIDE.value


def operator_registration() -> ReviewAuthority:
    """Read current operator custody, independently of any request snapshot."""
    layer = read_user_layer()
    if layer is None or layer.review_authority is None:
        raise PrincipalMismatchError(
            "register principal_id, os_user and os_uid in user preferences [review_authority]"
        )
    return layer.review_authority


def registered_humans() -> tuple[AuthorityPrincipal, ...]:
    return operator_registration().registered_humans


def _registered_identity(principal_id: str | None = None) -> ReviewAuthority:
    registration = operator_registration()
    if principal_id is not None and principal_id != registration.principal_id:
        raise PrincipalMismatchError("the requested principal is not the registered operator")
    if registration.os_uid != os.getuid() or registration.os_user != getpass.getuser():
        raise PrincipalMismatchError("the registered operator does not match the current OS user")
    return registration


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _authenticate(principal_id: str | None) -> tuple[ReviewAuthority, AuthenticatedDecisionContext]:
    if not _interactive():
        raise AuthenticationError("decisions require interactive stdin and stdout terminals")
    registration = _registered_identity(principal_id)
    try:
        tty_name = os.ttyname(sys.stdin.fileno())
        output_tty = os.ttyname(sys.stdout.fileno())
    except (OSError, ValueError) as error:
        raise AuthenticationError("a real local terminal is required") from error
    if tty_name != output_tty:
        raise AuthenticationError("decision input and output must use the same local terminal")
    context = AuthenticatedDecisionContext(registration.principal, os.getuid(), tty_name)
    authenticate_context(context, registration.registered_humans)
    return registration, context


def _state(ledger: LedgerService, run_id: str, node_id: str) -> Mapping[str, object]:
    projection = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:{node_id}",
    )
    state = {} if projection is None else projection.state
    bounded_json(state)
    return state


def _waits(ledger: LedgerService, request: Stage1RunRequest) -> tuple[str, ...]:
    return tuple(
        str(node_id) for node_id in request.manifest.nodes
        if _state(ledger, str(request.run.id), str(node_id)).get("status") == "awaiting_human"
    )


def _actions(ledger: LedgerService, request: Stage1RunRequest) -> tuple[str, ...]:
    waiting = _waits(ledger, request)
    actions: list[str] = []
    if "open_pr" in waiting or (
        not waiting
        and _state(ledger, str(request.run.id), "review").get("status") == "passed"
        and _state(ledger, str(request.run.id), "open_pr").get("status") not in {
            "passed", "failed", "cancelled", "blocked",
        }
    ):
        actions.append(_PUBLICATION)
    if any(node_id != "open_pr" for node_id in waiting):
        actions.append(_OVERRIDE)
    return tuple(actions)


def _override_node(ledger: LedgerService, request: Stage1RunRequest) -> str:
    waiting = tuple(node for node in _waits(ledger, request) if node != "open_pr")
    if len(waiting) != 1:
        raise ActionMismatchError("policy.override requires exactly one current human-wait node")
    return waiting[0]


def _schema(
    ledger: LedgerService, request: Stage1RunRequest, packet: ReviewPacket, action: str,
    *, node_id: str | None = None, reason: str = "", expires_at: datetime | None = None,
) -> ApprovalSchema:
    if action not in {_PUBLICATION, _OVERRIDE}:
        raise ActionMismatchError("factory decisions authorize publication or a scoped human wait only")
    if action == _OVERRIDE and not reason.strip():
        raise HardRuleError("a policy override requires a non-blank reason")
    current_attempt = _state(ledger, str(request.run.id), "implement").get("attempt_id")
    producers = {request.implementation.attempt_id}
    if isinstance(current_attempt, str):
        producers.add(current_attempt)
    schema = ApprovalSchema(
        action=PolicyAction(action), repository=request.repository_id,
        workflow_version=str(request.manifest.content_digest),
        policy_version=str(request.run.configuration_snapshot_digest),
        work_snapshot=packet.subject_digest,
        effective_configuration=packet.configuration_snapshot_digest,
        target_resource=str(request.run.id), diff_or_artifact_digest=packet.diff_digest,
        acceptance_criteria_snapshot=str(request.work_snapshot.bound_digest),
        evidence_bundle_digest=str(Digest.of_json({
            "receipts": list(packet.evidence_receipt_digests),
            "ci": packet.ci_evidence_json,
        })),
        requesting_principal_id=request.implementation.attempt_id,
        producer_principal_ids=tuple(sorted(producers)),
        override_reason=reason if action == _OVERRIDE else None,
        override_scope=(node_id,) if action == _OVERRIDE and node_id is not None else None,
        override_expires_at=expires_at if action == _OVERRIDE else None,
    )
    HardRuleEnforcer.enforce_request(schema)
    return schema


def issue_for_review(
    project: Path, run_id: str, report: dict[str, object], *, principal_id: str | None = None,
) -> dict[str, object]:
    """Add a one-time secret only to the immediate terminal report, never the packet."""
    result = dict(report)
    lines = list(report.get("lines", [])) if isinstance(report.get("lines"), list) else []
    if not _interactive():
        result["challenge_issued"] = False
        lines.append(
            "No decision challenge issued: interactive stdin and stdout terminals are required. "
            "The terminal review and HTML packet remain display-only alternatives."
        )
        result["lines"] = lines
        return result
    registration, context = _authenticate(principal_id)
    ledger, request = _open(project, run_id, writable=True)
    try:
        packet = build_packet(project, ledger, request)
        if report.get("subject_digest") != packet.subject_digest:
            raise SubjectMismatchError("the reviewed packet changed; run factory review again")
        actions = _actions(ledger, request)
        if not actions:
            result["challenge_issued"] = False
            lines.append("No decision challenge issued: this run has no currently available action.")
        else:
            issued = issue_challenge(
                ledger.connection, run_id=run_id, subject_fields=packet.subject_fields(),
                actions=actions, context=context, registered_humans=registration.registered_humans,
                lifetime=timedelta(seconds=registration.ttl_seconds),
            )
            result.update({
                "challenge_issued": True, "challenge": issued.challenge,
                "subject_digest": issued.subject_digest, "actions": list(issued.actions),
                "expires_at": issued.expires_at.isoformat(), "principal_id": context.principal.id,
            })
            lines.extend((
                f"Decision challenge: {issued.challenge}",
                f"Decision actions: {', '.join(issued.actions)}",
                f"Decision expires: {issued.expires_at.isoformat()}",
                f"Decision principal: {escape_terminal(context.principal.id)}",
            ))
        result["lines"] = lines
        return result
    finally:
        ledger.close()


def decide(
    project: Path, run_id: str, *, action: str, subject_digest: str, challenge: str,
    principal_id: str | None = None, reason: str = "",
) -> dict[str, object]:
    """Authenticate, rebuild current truth, and atomically consume one terminal challenge."""
    registration, context = _authenticate(principal_id)
    bounded_json(reason)
    assert_no_raw_credentials(reason)
    ledger, request = _open(project, run_id, writable=True)
    try:
        packet = build_packet(project, ledger, request)
        node_id: str | None = None

        def verify(decided_at: datetime, expires_at: datetime) -> HumanDecision:
            nonlocal node_id
            current = build_packet(project, ledger, request)
            if current.subject_digest != packet.subject_digest:
                raise StaleSubjectError("the packet changed while recording the decision")
            if action not in _actions(ledger, request):
                raise ActionMismatchError("the action is no longer available for this run")
            node_id = _override_node(ledger, request) if action == _OVERRIDE else None
            if _registered_identity(context.principal.id) != registration:
                raise PrincipalMismatchError("operator registration changed during the decision")
            schema = _schema(
                ledger, request, packet, action, node_id=node_id, reason=reason,
                expires_at=expires_at,
            )
            return decision_verifier(
                run_id=run_id, action=action, subject_fields=packet.subject_fields(),
                context=context, registered_humans=registration.registered_humans,
                schema=schema, reason=reason,
            )(decided_at, expires_at)

        decision = consume_decision(
            ledger.connection, run_id=run_id, action=action, subject_digest=subject_digest,
            current_subject_fields=packet.subject_fields(), challenge=challenge, context=context,
            registered_humans=registration.registered_humans, verifier=verify,
        )
        return {
            "run_id": run_id, "decision_id": decision.id, "action": decision.action,
            "outcome": "approved", "subject_digest": decision.subject_digest,
            "expires_at": decision.expires_at.isoformat(), "node_id": node_id,
        }
    finally:
        ledger.close()


def require_for_effect(
    ledger: LedgerService, request: Stage1RunRequest, action: str, node_id: str | None = None,
) -> HumanDecision:
    """Check durable authority against current truth; never require or manufacture a TTY."""
    packet = build_packet(request.repository_path, ledger, request)
    decision = require_current_decision(
        ledger.connection, str(request.run.id), action, packet.subject_digest,
    )
    registration = _registered_identity()
    if (
        decision.principal not in registration.registered_humans
        or decision.os_uid != registration.os_uid
    ):
        raise PrincipalMismatchError("the stored decision no longer matches operator registration")
    if decision.expires_at > decision.decided_at + timedelta(seconds=registration.ttl_seconds):
        raise AuthenticationError("the stored decision exceeds the current operator lifetime limit")
    if action == _OVERRIDE and node_id is None:
        raise ActionMismatchError("a policy override effect requires its exact node scope")
    if action == _PUBLICATION and node_id not in {None, "open_pr"}:
        raise ActionMismatchError("a publication decision cannot authorize another effect")
    schema = _schema(
        ledger, request, packet, action, node_id=node_id, reason=decision.reason,
        expires_at=decision.expires_at,
    )
    HardRuleEnforcer.enforce_approval(schema, (decision.principal,), (registration.principal_id,))
    if schema.digest() != decision.schema_digest:
        raise SubjectMismatchError("the stored decision does not match the exact current effect schema")
    return decision
