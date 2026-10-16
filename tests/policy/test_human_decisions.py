from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.application.human_decisions import (
    ActionMismatchError,
    AuthenticatedDecisionContext,
    AuthenticationError,
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
)
from enginery.domain.digests import Digest
from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.ledger.connection import open_connection
from enginery.ledger.human_decisions import (
    consume_decision,
    issue_challenge,
    list_decisions,
    require_current_decision,
)
from enginery.ledger.migrations import apply_pending_migrations
from enginery.policy.human_decisions import decision_verifier
from enginery.policy.rules import HardRuleError
from enginery.policy.schemas import ApprovalSchema

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
ACTION = PolicyAction.PULL_REQUEST_OPEN.value
OPERATOR = AuthorityPrincipal("operator", PrincipalType.HUMAN, "operator", "user-preferences")
OTHER = AuthorityPrincipal("other", PrincipalType.HUMAN, "operator", "user-preferences")
CONTEXT = AuthenticatedDecisionContext(OPERATOR, os.getuid(), "/dev/ttys001")
REGISTERED = (OPERATOR, OTHER)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    ledger = open_connection(tmp_path / "ledger.sqlite")
    apply_pending_migrations(ledger)
    yield ledger
    ledger.close()


def _subject(head: str = "head") -> dict[str, object]:
    return {
        "run_id": "run-1", "request_digest": "request", "configuration_snapshot_digest": "config",
        "base_revision": "base", "head_revision": head, "diff_digest": "diff",
        "evidence_receipt_digests": ["receipt"], "ci_evidence_digest": "ci", "findings": [],
    }


def _issue(
    connection: sqlite3.Connection, *, fields: dict[str, object] | None = None,
    lifetime: timedelta = timedelta(minutes=30),
) -> IssuedChallenge:
    return issue_challenge(
        connection, run_id="run-1", subject_fields=fields or _subject(), actions=(ACTION,),
        context=CONTEXT, registered_humans=REGISTERED, now=NOW, lifetime=lifetime,
    )


def _consume(
    connection: sqlite3.Connection, issued: IssuedChallenge, *,
    fields: dict[str, object] | None = None, context: AuthenticatedDecisionContext = CONTEXT,
    action: str = ACTION, digest: str | None = None, now: datetime = NOW,
    schema: ApprovalSchema | None = None, reason: str = "",
) -> HumanDecision:
    fields = fields or _subject()
    schema = schema or ApprovalSchema(
        action=PolicyAction(action), target_resource="run-1",
        work_snapshot=str(Digest.of_json(fields)), requesting_principal_id="worker",
        producer_principal_ids=("worker",),
    )
    return consume_decision(
        connection, run_id="run-1", action=action,
        subject_digest=digest or issued.subject_digest, current_subject_fields=fields,
        challenge=issued.challenge, context=context, registered_humans=REGISTERED, now=now,
        verifier=decision_verifier(
            run_id="run-1", action=action, subject_fields=fields, context=context,
            registered_humans=REGISTERED, schema=schema, reason=reason,
        ),
    )


def test_challenge_is_hash_only_and_success_consumes_once(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    assert len(bytes.fromhex(issued.challenge)) == 32
    row = connection.execute("SELECT * FROM human_decision_challenges").fetchone()
    assert row["challenge_hash"] == hashlib.sha256(issued.challenge.encode()).hexdigest()
    assert issued.challenge not in repr(tuple(row))
    decision = _consume(connection, issued)
    assert decision == require_current_decision(
        connection, "run-1", ACTION, issued.subject_digest, now=NOW,
    )
    assert decision.attestation().approved
    assert decision.normalized_inputs["work_snapshot"] == issued.subject_digest
    with pytest.raises(ReplayedChallengeError, match="already been consumed"):
        _consume(connection, issued)
    assert len(list_decisions(connection, "run-1")) == 1
    assert issued.challenge not in "\n".join(connection.iterdump())


@pytest.mark.parametrize(
    "context,error",
    [
        (replace(CONTEXT, principal=replace(OPERATOR, role="forged")), PrincipalMismatchError),
        (replace(CONTEXT, principal=OTHER), PrincipalMismatchError),
        (replace(CONTEXT, os_uid=os.getuid() + 1), AuthenticationError),
        (replace(CONTEXT, tty_name="http://localhost/decision"), AuthenticationError),
        (replace(CONTEXT, tty_name="/dev/ttys002"), PrincipalMismatchError),
        (replace(CONTEXT, principal=replace(OPERATOR, principal_type=PrincipalType.AGENT)),
         AuthenticationError),
    ],
)
def test_forged_principal_uid_tty_and_http_contexts_are_refused(
    connection: sqlite3.Connection, context: AuthenticatedDecisionContext,
    error: type[HumanDecisionError],
) -> None:
    issued = _issue(connection)
    with pytest.raises(error):
        _consume(connection, issued, context=context)
    assert list_decisions(connection, "run-1") == ()
    _consume(connection, issued)


def test_wrong_action_and_digest_leave_challenge_unused(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    with pytest.raises(ActionMismatchError, match="does not authorize this action"):
        _consume(connection, issued, action=PolicyAction.POLICY_OVERRIDE.value)
    with pytest.raises(SubjectMismatchError, match="submitted digest"):
        _consume(connection, issued, digest=str(Digest.of_json("forged")))
    assert list_decisions(connection, "run-1") == ()
    _consume(connection, issued)


@pytest.mark.parametrize("seconds", [1800, 1801])
def test_expiry_boundary_is_mandatory(connection: sqlite3.Connection, seconds: int) -> None:
    issued = _issue(connection)
    with pytest.raises(ExpiredChallengeError, match="expired"):
        _consume(connection, issued, now=NOW + timedelta(seconds=seconds))
    assert list_decisions(connection, "run-1") == ()


@pytest.mark.parametrize("lifetime", [timedelta(0), timedelta(minutes=31)])
def test_challenge_lifetime_cannot_disable_or_extend_expiry(
    connection: sqlite3.Connection, lifetime: timedelta,
) -> None:
    with pytest.raises(HumanDecisionError, match="at most 30 minutes"):
        _issue(connection, lifetime=lifetime)


def test_changed_subject_rejects_challenge(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    with pytest.raises(StaleSubjectError, match="no longer current"):
        _consume(connection, issued, fields=_subject("new-head"))
    with pytest.raises(StaleSubjectError):
        _consume(connection, issued)
    assert list_decisions(connection, "run-1") == ()


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("head_revision", "new-head"), ("base_revision", "new-base"),
        ("request_digest", "new-request"), ("configuration_snapshot_digest", "new-config"),
        ("diff_digest", "new-diff"), ("evidence_receipt_digests", ["new-receipt"]),
        ("ci_evidence_digest", "new-ci"), ("findings", ["new-finding"]),
    ],
)
def test_every_changed_bound_input_supersedes_history_without_new_decision(
    connection: sqlite3.Connection, field: str, new_value: object,
) -> None:
    issued = _issue(connection)
    original = _consume(connection, issued)
    fields = {**_subject(), field: new_value}
    new_digest = str(Digest.of_json(fields))
    with pytest.raises(SupersededDecisionError):
        require_current_decision(connection, "run-1", ACTION, new_digest, now=NOW)
    history = list_decisions(connection, "run-1")
    assert len(history) == 1
    assert history[0].id == original.id
    assert history[0].superseded_by == new_digest
    with pytest.raises(SupersededDecisionError):
        require_current_decision(connection, "run-1", ACTION, issued.subject_digest, now=NOW)
    fresh = _issue(connection, fields=fields)
    replacement = _consume(connection, fresh, fields=fields)
    assert replacement.id != original.id
    assert len(list_decisions(connection, "run-1")) == 2


def test_decision_expires_and_wrong_action_has_no_authority(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    _consume(connection, issued)
    with pytest.raises(ExpiredDecisionError):
        require_current_decision(
            connection, "run-1", ACTION, issued.subject_digest, now=issued.expires_at,
        )
    with pytest.raises(MissingDecisionError):
        require_current_decision(
            connection, "run-1", PolicyAction.POLICY_OVERRIDE.value, issued.subject_digest, now=NOW,
        )


def test_existing_registry_refuses_self_approval_without_consuming(
    connection: sqlite3.Connection,
) -> None:
    issued = _issue(connection)
    schema = ApprovalSchema(
        action=PolicyAction.PULL_REQUEST_OPEN, target_resource="run-1",
        work_snapshot=issued.subject_digest, requesting_principal_id=OPERATOR.id,
    )
    with pytest.raises(HardRuleError, match="producer separation"):
        _consume(connection, issued, schema=schema)
    assert list_decisions(connection, "run-1") == ()
    _consume(connection, issued)


def test_existing_override_hard_rule_cannot_be_relaxed(connection: sqlite3.Connection) -> None:
    action = PolicyAction.POLICY_OVERRIDE.value
    issued = issue_challenge(
        connection, run_id="run-1", subject_fields=_subject(), actions=(action,),
        context=CONTEXT, registered_humans=REGISTERED, now=NOW,
    )
    schema = ApprovalSchema(
        action=PolicyAction.POLICY_OVERRIDE, target_resource="run-1",
        work_snapshot=issued.subject_digest, requesting_principal_id="worker",
        override_reason="try bypass", override_scope=("approval_digest",),
        override_expires_at=issued.expires_at,
    )
    with pytest.raises(HardRuleError, match="cannot relax a hard rule"):
        _consume(connection, issued, action=action, schema=schema)
    assert list_decisions(connection, "run-1") == ()


def test_failed_decision_insert_rolls_back_nonce_consumption(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    connection.execute(
        "CREATE TRIGGER refuse_decision BEFORE INSERT ON human_decisions "
        "BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated persistence failure"):
        _consume(connection, issued)
    assert connection.execute(
        "SELECT consumed_at FROM human_decision_challenges"
    ).fetchone()["consumed_at"] is None
    assert list_decisions(connection, "run-1") == ()
    connection.execute("DROP TRIGGER refuse_decision")
    _consume(connection, issued)


def test_plaintext_cannot_leak_through_decision_reason(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    with pytest.raises(HumanDecisionError, match="plaintext terminal challenges"):
        _consume(connection, issued, reason=issued.challenge)
    assert list_decisions(connection, "run-1") == ()
    assert issued.challenge not in "\n".join(connection.iterdump())
    _consume(connection, issued)


def test_challenges_and_approved_decisions_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    connection = open_connection(path)
    apply_pending_migrations(connection)
    issued = _issue(connection)
    connection.close()
    connection = open_connection(path)
    try:
        original = _consume(connection, issued)
    finally:
        connection.close()
    connection = open_connection(path)
    try:
        restored = require_current_decision(
            connection, "run-1", ACTION, issued.subject_digest, now=NOW,
        )
        assert restored == original
        assert restored.schema_digest == original.schema_digest
        assert restored.normalized_inputs == original.normalized_inputs
        with pytest.raises(ReplayedChallengeError):
            _consume(connection, issued)
    finally:
        connection.close()


def test_nonce_write_failure_rolls_back_inserted_decision(connection: sqlite3.Connection) -> None:
    issued = _issue(connection)
    connection.execute(
        "CREATE TRIGGER refuse_consumption BEFORE UPDATE OF consumed_at "
        "ON human_decision_challenges "
        "BEGIN SELECT RAISE(ABORT, 'simulated consumption failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated consumption failure"):
        _consume(connection, issued)
    assert list_decisions(connection, "run-1") == ()
    assert connection.execute(
        "SELECT consumed_at FROM human_decision_challenges"
    ).fetchone()["consumed_at"] is None
    connection.execute("DROP TRIGGER refuse_consumption")
    _consume(connection, issued)


def test_incomplete_subject_cannot_be_challenged(connection: sqlite3.Connection) -> None:
    with pytest.raises(SubjectMismatchError, match="every packet input"):
        _issue(connection, fields={"run_id": "run-1"})


def test_unregistered_human_cannot_issue_challenge(connection: sqlite3.Connection) -> None:
    with pytest.raises(PrincipalMismatchError, match="not registered"):
        issue_challenge(
            connection, run_id="run-1", subject_fields=_subject(), actions=(ACTION,),
            context=replace(CONTEXT, principal=replace(OPERATOR, id="unregistered")),
            registered_humans=REGISTERED, now=NOW,
        )
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM human_decision_challenges"
    ).fetchone()["count"] == 0
