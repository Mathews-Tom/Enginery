"""Hashed challenges and atomic approved decisions in the existing project ledger."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from enginery.application.human_decisions import (
    ActionMismatchError,
    AuthenticatedDecisionContext,
    ExpiredChallengeError,
    ExpiredDecisionError,
    HumanDecision,
    HumanDecisionError,
    IssuedChallenge,
    MissingDecisionError,
    PrincipalMismatchError,
    ReplayedChallengeError,
    StaleSubjectError,
    SubjectMismatchError,
    SupersededDecisionError,
    UnknownChallengeError,
    authenticate_context,
    bounded_json,
    checked_time,
    subject_digest as compute_subject_digest,
    policy_action,
)
from enginery.domain.digests import Digest
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.ledger.connection import transaction

MAX_CHALLENGE_LIFETIME = timedelta(minutes=30)
_NONCE = re.compile(r"[0-9a-f]{64}\Z")


def _principal_json(principal: AuthorityPrincipal) -> str:
    return bounded_json({**asdict(principal), "principal_type": principal.principal_type.value})


def _supersede(connection: sqlite3.Connection, run_id: str, current_subject: str) -> None:
    for table in ("human_decisions", "human_decision_challenges"):
        connection.execute(
            f"UPDATE {table} SET superseded_by = ? "
            "WHERE run_id = ? AND subject_digest != ? AND superseded_by IS NULL",
            (current_subject, run_id, current_subject),
        )


def issue_challenge(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    subject_fields: Mapping[str, object],
    actions: tuple[str, ...],
    context: AuthenticatedDecisionContext,
    registered_humans: tuple[AuthorityPrincipal, ...],
    lifetime: timedelta = MAX_CHALLENGE_LIFETIME,
    now: datetime | None = None,
) -> IssuedChallenge:
    """Issue a random 256-bit terminal secret; persist only its SHA-256 hash."""
    authenticate_context(context, registered_humans)
    issued_at = checked_time(now or datetime.now(UTC))
    if not timedelta(0) < lifetime <= MAX_CHALLENGE_LIFETIME:
        raise HumanDecisionError("challenge lifetime must be positive and at most 30 minutes")
    if not actions or len(actions) != len(set(actions)):
        raise ActionMismatchError("a challenge requires a nonempty unique action set")
    for action in actions:
        try:
            policy_action(action)
        except ValueError as error:
            raise ActionMismatchError("the challenge action is outside the policy namespace") from error
    digest = compute_subject_digest(run_id, subject_fields)
    fields_json = bounded_json(subject_fields)
    challenge = secrets.token_hex(32)
    challenge_hash = hashlib.sha256(challenge.encode()).hexdigest()
    expires_at = issued_at + lifetime
    with transaction(connection):
        _supersede(connection, run_id, digest)
        connection.execute(
            "INSERT INTO human_decision_challenges "
            "(challenge_hash, run_id, principal_json, os_uid, tty_name, actions_json, "
            "subject_digest, subject_fields_json, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (challenge_hash, run_id, _principal_json(context.principal), context.os_uid,
             context.tty_name, bounded_json(sorted(actions)), digest, fields_json,
             issued_at.isoformat(), expires_at.isoformat()),
        )
    return IssuedChallenge(challenge, digest, actions, expires_at)


def consume_decision(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    action: str,
    subject_digest: str,
    current_subject_fields: Mapping[str, object],
    challenge: str,
    context: AuthenticatedDecisionContext,
    registered_humans: tuple[AuthorityPrincipal, ...],
    verifier: Callable[[datetime, datetime], HumanDecision],
    now: datetime | None = None,
    release: Callable[[HumanDecision], None] | None = None,
) -> HumanDecision:
    """Verify and consume the nonce with the approval write under one write lock.

    Policy is injected, never imported by persistence. Refused stale subjects still
    commit supersession, while verifier/write failures roll the entire consume back.
    No diagnostics or stored inputs may contain the plaintext terminal challenge.
    """
    authenticate_context(context, registered_humans)
    decided_at = checked_time(now or datetime.now(UTC))
    current_digest = compute_subject_digest(run_id, current_subject_fields)
    if not _NONCE.fullmatch(challenge):
        raise UnknownChallengeError("the terminal challenge is unknown")
    challenge_hash = hashlib.sha256(challenge.encode()).hexdigest()
    refusal: HumanDecisionError | None = None
    decision: HumanDecision | None = None
    with transaction(connection):
        _supersede(connection, run_id, current_digest)
        row = connection.execute(
            "SELECT * FROM human_decision_challenges WHERE challenge_hash = ?",
            (challenge_hash,),
        ).fetchone()
        if row is None or row["run_id"] != run_id:
            refusal = UnknownChallengeError("the terminal challenge is unknown for this run")
        elif row["principal_json"] != _principal_json(context.principal):
            refusal = PrincipalMismatchError("the challenge belongs to another principal")
        elif row["os_uid"] != context.os_uid or row["tty_name"] != context.tty_name:
            refusal = PrincipalMismatchError("the challenge belongs to another terminal identity")
        elif action not in json.loads(row["actions_json"]):
            refusal = ActionMismatchError("the challenge does not authorize this action")
        elif row["consumed_at"] is not None:
            refusal = ReplayedChallengeError("the terminal challenge has already been consumed")
        elif row["superseded_by"] is not None or row["subject_digest"] != current_digest:
            refusal = StaleSubjectError("the challenge subject is no longer current")
        elif subject_digest != row["subject_digest"]:
            refusal = SubjectMismatchError("the submitted digest does not match the challenge")
        elif decided_at >= datetime.fromisoformat(row["expires_at"]):
            refusal = ExpiredChallengeError("the terminal challenge has expired")
        elif decided_at < datetime.fromisoformat(row["issued_at"]):
            refusal = ExpiredChallengeError("the terminal challenge is not yet valid")
        else:
            expiry = datetime.fromisoformat(row["expires_at"])
            decision = verifier(decided_at, expiry)
            if (
                decision.run_id != run_id or decision.action != action
                or decision.subject_digest != current_digest
                or decision.principal != context.principal
                or decision.os_uid != context.os_uid or decision.tty_name != context.tty_name
                or decision.decided_at != decided_at or decision.expires_at > expiry
                or decision.superseded_by is not None
            ):
                raise SubjectMismatchError("the verified approval does not bind this challenge")
            inputs = bounded_json(decision.normalized_inputs)
            fields = bounded_json(decision.subject_fields)
            principal = _principal_json(decision.principal)
            if challenge in bounded_json((inputs, fields, principal, decision.reason)):
                raise HumanDecisionError("plaintext terminal challenges cannot be retained in decisions")
            connection.execute(
                "INSERT INTO human_decisions "
                "(decision_id, challenge_hash, run_id, principal_id, principal_json, action, "
                "subject_digest, subject_fields_json, os_uid, tty_name, decided_at, expires_at, "
                "schema_digest, normalized_inputs_json, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision.id, challenge_hash, run_id, decision.principal.id, principal, action,
                 current_digest, fields, context.os_uid, context.tty_name,
                 decided_at.isoformat(), decision.expires_at.isoformat(),
                 str(decision.schema_digest), inputs, decision.reason),
            )
            connection.execute(
                "UPDATE human_decision_challenges SET consumed_at = ?, decision_id = ? "
                "WHERE challenge_hash = ? AND consumed_at IS NULL",
                (decided_at.isoformat(), decision.id, challenge_hash),
            )
            if release is not None:
                release(decision)
    if refusal is not None:
        raise refusal
    if decision is None:
        raise MissingDecisionError("a verified decision was not recorded")
    return decision


def _decision_from_row(row: sqlite3.Row) -> HumanDecision:
    principal = json.loads(row["principal_json"])
    algorithm, hex_value = row["schema_digest"].split(":", 1)
    return HumanDecision(
        id=row["decision_id"], run_id=row["run_id"],
        principal=AuthorityPrincipal(
            principal["id"], PrincipalType(principal["principal_type"]),
            principal["role"], principal["authorization_source"],
        ),
        action=row["action"], subject_digest=row["subject_digest"],
        subject_fields=json.loads(row["subject_fields_json"]), os_uid=row["os_uid"],
        tty_name=row["tty_name"], decided_at=datetime.fromisoformat(row["decided_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        schema_digest=Digest(algorithm, hex_value),
        normalized_inputs=json.loads(row["normalized_inputs_json"]),
        reason=row["reason"], superseded_by=row["superseded_by"],
    )


def list_decisions(connection: sqlite3.Connection, run_id: str) -> tuple[HumanDecision, ...]:
    """Retain all decisions for audit, including expired and superseded history."""
    rows = connection.execute(
        "SELECT * FROM human_decisions WHERE run_id = ? ORDER BY rowid", (run_id,),
    ).fetchall()
    return tuple(_decision_from_row(row) for row in rows)


def require_current_decision(
    connection: sqlite3.Connection,
    run_id: str,
    action: str,
    subject_digest: str,
    *,
    now: datetime | None = None,
) -> HumanDecision:
    """Recheck current durable authority and retire changed subjects even on refusal."""
    reference_time = checked_time(now or datetime.now(UTC))
    with transaction(connection):
        _supersede(connection, run_id, subject_digest)
        rows = connection.execute(
            "SELECT * FROM human_decisions WHERE run_id = ? AND action = ? ORDER BY rowid DESC",
            (run_id, action),
        ).fetchall()
    if not rows:
        raise MissingDecisionError("an authenticated decision is required for this action")
    for row in rows:
        if row["subject_digest"] != subject_digest or row["superseded_by"] is not None:
            continue
        decision = _decision_from_row(row)
        if reference_time >= decision.expires_at or reference_time < decision.decided_at:
            raise ExpiredDecisionError("the authenticated action decision has expired")
        return decision
    raise SupersededDecisionError("the action decision was superseded by a changed subject")
