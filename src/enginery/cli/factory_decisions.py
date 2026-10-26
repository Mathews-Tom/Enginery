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
    HumanDecisionError,
    PrincipalMismatchError,
    ReviewAuthority,
    StaleSubjectError,
    SubjectMismatchError,
    authenticate_context,
    bounded_json,
    decision_commands,
    policy_action,
    wait_scope,
)
from enginery.application.review_packet import ReviewPacket
from enginery.cli.config_locations import read_user_layer
from enginery.cli.factory_review import _open, build_packet, preimplementation_waits
from enginery.domain.digests import Digest
from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE
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
from enginery.workflows.run_lifecycle import (
    Stage1RunRequest,
    behavioral_repair_exhausted,
    github_outcome_terminal,
)

_PUBLICATION = PolicyAction.PULL_REQUEST_OPEN.value


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
        raise PrincipalMismatchError(f"select registered principal id: {registration.principal_id}")
    if registration.os_uid != os.getuid() or registration.os_user != getpass.getuser():
        raise PrincipalMismatchError("register the current OS user and uid in user preferences")
    return registration


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _authenticate(principal_id: str | None) -> tuple[ReviewAuthority, AuthenticatedDecisionContext]:
    if any(key in os.environ for key in ("HTTP_ORIGIN", "HTTP_REFERER", "REQUEST_METHOD")):
        raise AuthenticationError(
            "HTTP-originated decisions are not supported; use the local terminal"
        )
    if not _interactive():
        raise AuthenticationError("run enginery review and decide in the same interactive terminal")
    registration = _registered_identity(principal_id)
    try:
        tty_name = os.ttyname(sys.stdin.fileno())
        output_tty = os.ttyname(sys.stdout.fileno())
    except (OSError, ValueError) as error:
        raise AuthenticationError(
            "run enginery review and decide in a real local terminal"
        ) from error
    if tty_name != output_tty:
        raise AuthenticationError("decision input and output must use the same local terminal")
    context = AuthenticatedDecisionContext(registration.principal, os.getuid(), tty_name)
    authenticate_context(context, registration.registered_humans)
    return registration, context


def _state(ledger: LedgerService, run_id: str, node_id: str) -> Mapping[str, object]:
    projection = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
        aggregate_id=f"{run_id}:{node_id}",
    )
    state = {} if projection is None else projection.state
    bounded_json(state)
    return state


def _waits(ledger: LedgerService, request: Stage1RunRequest) -> tuple[str, ...]:
    return tuple(
        str(node_id)
        for node_id in request.manifest.nodes
        if _state(ledger, str(request.run.id), str(node_id)).get("status") == "awaiting_human"
    )


def _actions(ledger: LedgerService, request: Stage1RunRequest) -> tuple[str, ...]:
    if behavioral_repair_exhausted(
        request,
        _state(ledger, str(request.run.id), "implement"),
        _state(ledger, str(request.run.id), "verify"),
    ):
        return ()
    if any(
        github_outcome_terminal(
            request,
            _state(ledger, str(request.run.id), "implement"),
            _state(ledger, str(request.run.id), node),
        )
        for node in ("wait_for_ci", "verify")
    ):
        return ()
    waiting = preimplementation_waits(ledger, request) or _waits(ledger, request)
    actions: list[str] = []
    publication = _state(ledger, str(request.run.id), "open_pr")
    if "open_pr" in waiting or (
        not waiting
        and _state(ledger, str(request.run.id), "review").get("status") == "passed"
        and publication.get("status") not in {"failed", "cancelled", "blocked"}
    ):
        actions.append(_PUBLICATION)
    for node_id in waiting:
        for outcome in ("approve", "reject"):
            if node_id == "open_pr" and outcome == "approve":
                continue
            actions.append(f"human_wait.{node_id}.{outcome}")
    return tuple(actions)


def _schema(
    ledger: LedgerService,
    request: Stage1RunRequest,
    packet: ReviewPacket,
    action: str,
    *,
    node_id: str | None = None,
    reason: str = "",
    expires_at: datetime | None = None,
) -> ApprovalSchema:

    scope = wait_scope(action)
    if action != _PUBLICATION and scope is None:
        raise ActionMismatchError("use pull_request.open or an exact human_wait.NODE action")
    if scope is not None and (node_id != scope[0] or not reason.strip()):
        raise HardRuleError("a node-scoped human-wait decision requires its node and a reason")
    current_attempt = _state(ledger, str(request.run.id), "implement").get("attempt_id")
    producers = {request.implementation.attempt_id}
    if isinstance(current_attempt, str):
        producers.add(current_attempt)
    schema = ApprovalSchema(
        action=policy_action(action),
        repository=request.repository_id,
        workflow_version=str(request.manifest.content_digest),
        policy_version=str(request.run.configuration_snapshot_digest),
        work_snapshot=packet.subject_digest,
        effective_configuration=packet.configuration_snapshot_digest,
        target_resource=str(request.run.id),
        diff_or_artifact_digest=packet.diff_digest,
        acceptance_criteria_snapshot=str(request.work_snapshot.bound_digest),
        evidence_bundle_digest=str(
            Digest.of_json(
                {
                    "receipts": list(packet.evidence_receipt_digests),
                    "ci": packet.ci_evidence_json,
                }
            )
        ),
        requesting_principal_id=request.implementation.attempt_id,
        producer_principal_ids=tuple(sorted(producers)),
        override_reason=reason if scope else None,
        override_scope=scope,
        override_expires_at=expires_at if scope else None,
    )
    HardRuleEnforcer.enforce_request(schema)
    return schema


def issue_for_review(
    project: Path,
    run_id: str,
    report: dict[str, object],
    *,
    principal_id: str | None = None,
    database: Path | None = None,
    owner: str = "factory",
) -> dict[str, object]:
    """Return a terminal-only secret separately from serializable report lines."""
    result = dict(report)
    if not _interactive():
        result["challenge_issued"] = False
        result["challenge_reason"] = "decision_authentication_required"
        return result
    try:
        registration, context = _authenticate(principal_id)
    except PrincipalMismatchError as error:
        result["challenge_issued"] = False
        result["challenge_reason"] = error.code
        notice = (
            f"No decision challenge: {error}. Register principal_id, os_user and os_uid "
            "in operator user preferences [review_authority], then run enginery review "
            "again in a real local terminal."
        )
        result["notice"] = notice
        lines = report.get("lines")
        result["lines"] = [*(lines if isinstance(lines, list) else []), notice]
        return result
    ledger, request = _open(project, run_id, writable=True, database=database, owner=owner)
    try:
        packet = build_packet(project, ledger, request)
        if report.get("subject_digest") != packet.subject_digest:
            raise SubjectMismatchError("the reviewed packet changed; run enginery review again")
        actions = _actions(ledger, request)
        if not actions:
            result["challenge_issued"] = False
            result["challenge_reason"] = "decision_action_mismatch"
        else:
            issued = issue_challenge(
                ledger.connection,
                run_id=run_id,
                subject_fields=packet.subject_fields(),
                actions=actions,
                context=context,
                registered_humans=registration.registered_humans,
                lifetime=timedelta(seconds=registration.ttl_seconds),
            )
            result.update(
                {
                    "challenge_issued": True,
                    "challenge": issued.challenge,
                    "subject_digest": issued.subject_digest,
                    "actions": list(issued.actions),
                    "expires_at": issued.expires_at.isoformat(),
                    "principal_id": context.principal.id,
                }
            )
            lines = report.get("lines")
            result["lines"] = [
                *(lines if isinstance(lines, list) else []),
                f"Decision actions: {', '.join(issued.actions)}",
                f"Decision expires: {issued.expires_at.isoformat()}",
                f"Decision principal: {context.principal.id}",
            ]
        return result
    finally:
        ledger.close()


def decide_in_ledger(
    ledger: LedgerService,
    request: Stage1RunRequest,
    *,
    action: str,
    subject_digest: str,
    challenge: str,
    principal_id: str | None = None,
    reason: str = "",
    owner: str = "factory",
) -> dict[str, object]:
    """Attach one repair using the actual ledger, owner and requested action."""
    try:
        return _decide_in_ledger(
            ledger,
            request,
            action=action,
            subject_digest=subject_digest,
            challenge=challenge,
            principal_id=principal_id,
            reason=reason,
            owner=owner,
        )
    except HumanDecisionError as error:
        database = ledger.connection.execute("PRAGMA database_list").fetchone()[2]
        review, decide = decision_commands(
            str(request.run.id),
            str(request.repository_path),
            action,
            database=database or None,
            owner=owner,
        )
        repair = (
            review
            if isinstance(error, ActionMismatchError) or action not in _actions(ledger, request)
            else f"{review}; {decide}"
        )
        raise type(error)(error.args[0], details={**error.details, "repair": repair}) from error


def _decide_in_ledger(
    ledger: LedgerService,
    request: Stage1RunRequest,
    *,
    action: str,
    subject_digest: str,
    challenge: str,
    principal_id: str | None,
    reason: str,
    owner: str,
) -> dict[str, object]:
    """Consume and release an exact wait under ONE write transaction, including its event."""
    scope = wait_scope(action)
    if scope is not None and scope[1] == "requeue":
        raise ActionMismatchError(
            "human_wait_requeue_unsupported: Stage 1 cannot complete a requeued human wait; "
            "the wait is unchanged",
            details={"reason": "human_wait_requeue_unsupported", "node_id": scope[0]},
        )
    registration, context = _authenticate(principal_id)
    bounded_json(reason)
    assert_no_raw_credentials(reason)
    run_id = str(request.run.id)
    packet = build_packet(request.repository_path, ledger, request)
    node_id = "open_pr" if action == _PUBLICATION else None if scope is None else scope[0]
    schema_expiry = timedelta(seconds=registration.ttl_seconds)

    def verify(decided_at: datetime, expires_at: datetime) -> HumanDecision:
        current = build_packet(request.repository_path, ledger, request)
        if current.subject_digest != packet.subject_digest:
            raise StaleSubjectError("the packet changed while recording; run enginery review again")
        if action not in _actions(ledger, request):
            raise ActionMismatchError(
                "the action is no longer available; run enginery review again"
            )
        if _registered_identity(context.principal.id) != registration:
            raise PrincipalMismatchError("operator registration changed; run enginery review again")
        schema = _schema(
            ledger,
            request,
            packet,
            action,
            node_id=node_id,
            reason=reason,
            expires_at=min(expires_at, decided_at + schema_expiry),
        )
        return decision_verifier(
            run_id=run_id,
            action=action,
            subject_fields=packet.subject_fields(),
            context=context,
            registered_humans=registration.registered_humans,
            schema=schema,
            reason=reason,
        )(decided_at, expires_at)

    def release(decision: HumanDecision) -> None:
        if node_id is None or node_id not in _waits(ledger, request):
            return
        from enginery.cli.stage1_composition import HEARTBEAT_WINDOW, runtime_for_run

        runtime = runtime_for_run(ledger, owner=owner, run_id=run_id)
        epoch = runtime.claim_epoch(now=decision.decided_at, heartbeat_window=HEARTBEAT_WINDOW)
        extra: dict[str, object] = {
            "decision_id": decision.id,
            "decision_action": action,
            "reason": decision.reason,
        }
        if not request.execution_selection.legacy:
            extra["execution_selection_digest"] = str(request.execution_selection.digest)
        runtime.resolve_human_wait(
            run_id=run_id,
            node_id=node_id,
            epoch=epoch.epoch,
            now=decision.decided_at,
            outcome="failed" if scope and scope[1] == "reject" else "passed",
            extra=extra,
        )

    decision = consume_decision(
        ledger.connection,
        run_id=run_id,
        action=action,
        subject_digest=subject_digest,
        current_subject_fields=packet.subject_fields(),
        challenge=challenge,
        context=context,
        registered_humans=registration.registered_humans,
        verifier=verify,
        release=release,
    )
    return {
        "run_id": run_id,
        "decision_id": decision.id,
        "action": decision.action,
        "outcome": "rejected" if scope and scope[1] == "reject" else "approved",
        "subject_digest": decision.subject_digest,
        "expires_at": decision.expires_at.isoformat(),
        "node_id": node_id,
    }


def decide(
    project: Path,
    run_id: str,
    *,
    action: str,
    subject_digest: str,
    challenge: str,
    principal_id: str | None = None,
    reason: str = "",
    database: Path | None = None,
    owner: str = "factory",
) -> dict[str, object]:
    ledger, request = _open(project, run_id, writable=True, database=database, owner=owner)
    try:
        return decide_in_ledger(
            ledger,
            request,
            action=action,
            subject_digest=subject_digest,
            challenge=challenge,
            principal_id=principal_id,
            reason=reason,
            owner=owner,
        )
    finally:
        ledger.close()


def require_for_effect(
    ledger: LedgerService,
    request: Stage1RunRequest,
    action: str,
    node_id: str | None = None,
) -> HumanDecision:
    """Recheck durable authority immediately before publication; no TTY grants authority here."""
    packet = build_packet(request.repository_path, ledger, request)
    decision = require_current_decision(
        ledger.connection,
        str(request.run.id),
        action,
        packet.subject_digest,
    )
    registration = _registered_identity()
    if (
        decision.principal not in registration.registered_humans
        or decision.os_uid != registration.os_uid
    ):
        raise PrincipalMismatchError("the decision no longer matches operator registration")
    if decision.expires_at > decision.decided_at + timedelta(seconds=registration.ttl_seconds):
        raise AuthenticationError("the decision exceeds the current operator lifetime limit")
    scope = wait_scope(action)
    if (scope and scope[0] != node_id) or (
        action == _PUBLICATION and node_id not in {None, "open_pr"}
    ):
        raise ActionMismatchError("the decision cannot authorize another node")
    schema = _schema(
        ledger,
        request,
        packet,
        action,
        node_id=node_id,
        reason=decision.reason,
        expires_at=decision.expires_at,
    )
    HardRuleEnforcer.enforce_approval(schema, (decision.principal,), (registration.principal_id,))
    if schema.digest() != decision.schema_digest:
        raise SubjectMismatchError("the decision does not match the exact current effect schema")
    return decision
