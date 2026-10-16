"""Authenticated terminal decision values shared by policy and ledger boundaries."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.immutable import freeze_json_mapping, thaw_json_value
from enginery.domain.policy_decision import ApprovalAttestation, PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType

MAX_DECISION_BYTES = 1024 * 1024


class HumanDecisionError(InvalidInputError):
    """A refused terminal decision with a stable, non-secret diagnostic code."""

    code: ClassVar[str] = "human_decision_refused"

    def __str__(self) -> str:
        return (
            f"{self.code}: {super().__str__()}; repair: in the registered operator's terminal, "
            "run enginery factory review RUN --project PROJECT, then enginery factory decide "
            "RUN --project PROJECT --action ACTION --subject-digest DIGEST --challenge CHALLENGE"
        )


class AuthenticationError(HumanDecisionError):
    code = "decision_authentication_required"


class PrincipalMismatchError(HumanDecisionError):
    code = "decision_principal_mismatch"


class ActionMismatchError(HumanDecisionError):
    code = "decision_action_mismatch"


class SubjectMismatchError(HumanDecisionError):
    code = "decision_subject_mismatch"


class StaleSubjectError(HumanDecisionError):
    code = "decision_subject_stale"


class UnknownChallengeError(HumanDecisionError):
    code = "decision_challenge_unknown"


class ExpiredChallengeError(HumanDecisionError):
    code = "decision_challenge_expired"


class ReplayedChallengeError(HumanDecisionError):
    code = "decision_challenge_replayed"


class MissingDecisionError(HumanDecisionError):
    code = "decision_required"


class ExpiredDecisionError(HumanDecisionError):
    code = "decision_expired"


class SupersededDecisionError(HumanDecisionError):
    code = "decision_superseded"


@dataclass(frozen=True, slots=True)
class ReviewAuthority:
    """Operator-owned local registration, not an effective request behavior setting."""

    principal_id: str
    os_user: str
    os_uid: int
    ttl_seconds: int = 1800

    def __post_init__(self) -> None:
        for name, value in (("principal_id", self.principal_id), ("os_user", self.os_user)):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or len(value) > 128
                or any(ord(character) < 33 or ord(character) > 126 for character in value)
            ):
                raise HumanDecisionError(f"review_authority.{name} must be a plain identity")
        if type(self.os_uid) is not int or self.os_uid < 0:
            raise HumanDecisionError("review_authority.os_uid must be a nonnegative integer")
        if type(self.ttl_seconds) is not int or not 1 <= self.ttl_seconds <= 1800:
            raise HumanDecisionError(
                "review_authority.ttl_seconds must be an integer from 1 to 1800"
            )

    @property
    def principal(self) -> AuthorityPrincipal:
        return AuthorityPrincipal(
            self.principal_id,
            PrincipalType.HUMAN,
            "operator",
            "user-preferences",
        )

    @property
    def registered_humans(self) -> tuple[AuthorityPrincipal, ...]:
        return (self.principal,)


@dataclass(frozen=True, slots=True)
class AuthenticatedDecisionContext:
    """Trusted CLI identity; the ingress must additionally verify real isatty()."""

    principal: AuthorityPrincipal
    os_uid: int
    tty_name: str


def authenticate_context(
    context: AuthenticatedDecisionContext,
    registered_humans: tuple[AuthorityPrincipal, ...],
) -> None:
    """Refuse nonlocal or unregistered identities, even for ordinary PR actions."""
    if (
        context.principal.principal_type is not PrincipalType.HUMAN
        or not context.principal.id.strip()
        or context.os_uid != os.getuid()
        or not context.tty_name.startswith("/dev/")
        or context.tty_name in {"/dev/null", "/dev/zero"}
        or any(ord(char) < 32 or ord(char) >= 127 for char in context.tty_name)
    ):
        raise AuthenticationError(
            "a registered local human and authenticated terminal are required"
        )
    if context.principal not in registered_humans:
        raise PrincipalMismatchError("the authenticated human principal is not registered")


def bounded_json(value: object) -> str:
    """Canonical retained inputs, bounded before they enter the private ledger."""
    encoded = json.dumps(
        thaw_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if len(encoded.encode()) > MAX_DECISION_BYTES:
        raise HumanDecisionError("decision inputs exceed the 1 MiB limit")
    return encoded


def checked_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HumanDecisionError("decision timestamps must be timezone-aware")
    return value


def wait_scope(action: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"human_wait\.([A-Za-z0-9_.-]+)\.(approve|reject|requeue)", action)
    return None if match is None else (match[1], match[2])


def policy_action(action: str) -> PolicyAction:
    """Map an exact node/effect action onto the existing hard-rule namespace."""
    return PolicyAction.POLICY_OVERRIDE if wait_scope(action) else PolicyAction(action)


def subject_digest(run_id: str, fields: Mapping[str, object]) -> str:
    required = {
        "run_id",
        "request_digest",
        "configuration_snapshot_digest",
        "base_revision",
        "head_revision",
        "diff_digest",
        "evidence_receipt_digests",
        "ci_evidence_digest",
        "findings",
    }
    if not run_id.strip() or fields.get("run_id") != run_id or not required.issubset(fields):
        raise SubjectMismatchError("the decision subject must bind every packet input and this run")
    return str(Digest.of_json(json.loads(bounded_json(fields))))


@dataclass(frozen=True, slots=True)
class IssuedChallenge:
    """The plaintext exists only in the issuing caller's return value."""

    challenge: str
    subject_digest: str
    actions: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class HumanDecision:
    """Durable approved policy record plus its terminal and packet binding."""

    id: str
    run_id: str
    principal: AuthorityPrincipal
    action: str
    subject_digest: str
    subject_fields: Mapping[str, object]
    os_uid: int
    tty_name: str
    decided_at: datetime
    expires_at: datetime
    schema_digest: Digest
    normalized_inputs: Mapping[str, object]
    reason: str = ""
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        checked_time(self.decided_at)
        checked_time(self.expires_at)
        if self.expires_at <= self.decided_at:
            raise HumanDecisionError("decision expiry must follow the decision time")
        if subject_digest(self.run_id, self.subject_fields) != self.subject_digest:
            raise SubjectMismatchError("decision subject fields do not match their digest")
        if (
            self.normalized_inputs.get("action") != policy_action(self.action).value
            or self.normalized_inputs.get("work_snapshot") != self.subject_digest
            or self.normalized_inputs.get("target_resource") != self.run_id
            or Digest.of_json(thaw_json_value(self.normalized_inputs)) != self.schema_digest
        ):
            raise SubjectMismatchError("approved policy inputs do not bind this action and subject")
        policy_action(self.action)
        bounded_json(self.normalized_inputs)
        bounded_json(self.reason)
        freeze_json_mapping(self, "normalized_inputs", self.normalized_inputs)
        freeze_json_mapping(self, "subject_fields", self.subject_fields)

    def attestation(self) -> ApprovalAttestation:
        return ApprovalAttestation(
            action=policy_action(self.action),
            schema_digest=self.schema_digest,
            normalized_inputs=self.normalized_inputs,
            approvers=(self.principal,),
            approved=True,
            expires_at=self.expires_at,
            superseded=self.superseded_by is not None,
        )
