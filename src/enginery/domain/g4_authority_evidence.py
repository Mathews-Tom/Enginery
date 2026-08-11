"""Externally verified GitHub authority evidence for one G4 finding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class G4AuthorityEvidence:
    """A merged evidence pull request bound to two current distinct approvals."""

    finding_id: str
    pull_request_number: int
    merged_head_revision: str
    document_digest: Digest
    producer_github_user_id: int
    evidence_pull_request_author_github_user_id: int
    approver_principal_ids: tuple[str, str]
    approver_github_user_ids: tuple[int, int]
    verified_at: datetime

    def __post_init__(self) -> None:
        if not self.finding_id.strip() or not self.merged_head_revision.strip():
            raise InvalidInputError("G4 authority evidence requires finding and merged head")
        if self.pull_request_number < 1:
            raise InvalidInputError("G4 authority evidence pull request number must be positive")
        if len(set(self.approver_principal_ids)) != 2 or any(
            not principal_id.strip() for principal_id in self.approver_principal_ids
        ):
            raise InvalidInputError("G4 authority evidence requires two distinct approvers")
        github_user_ids = (
            self.producer_github_user_id,
            self.evidence_pull_request_author_github_user_id,
            *self.approver_github_user_ids,
        )
        if any(
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
            for user_id in github_user_ids
        ):
            raise InvalidInputError("G4 authority evidence requires positive GitHub user IDs")
        if len(set(github_user_ids)) != len(github_user_ids):
            raise InvalidInputError("G4 authority evidence requires distinct GitHub identities")

    def to_state(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "pull_request_number": self.pull_request_number,
            "merged_head_revision": self.merged_head_revision,
            "document_digest": str(self.document_digest),
            "producer_github_user_id": self.producer_github_user_id,
            "evidence_pull_request_author_github_user_id": (
                self.evidence_pull_request_author_github_user_id
            ),
            "approver_principal_ids": list(self.approver_principal_ids),
            "approver_github_user_ids": list(self.approver_github_user_ids),
            "verified_at": self.verified_at.isoformat(),
        }


def g4_authority_evidence_from_state(state: Mapping[str, object]) -> G4AuthorityEvidence:
    """Decode one immutable authority-evidence projection."""
    finding_id = _string(state, "finding_id")
    pull_request_number = _positive_int(state, "pull_request_number")
    merged_head_revision = _string(state, "merged_head_revision")
    document_digest = _digest(state, "document_digest")
    approver_values = state.get("approver_principal_ids")
    approver_user_values = state.get("approver_github_user_ids")
    if (
        not isinstance(approver_values, list)
        or len(approver_values) != 2
        or not all(isinstance(value, str) and value.strip() for value in approver_values)
    ):
        raise InvalidInputError(
            "G4 authority evidence approver_principal_ids must contain two non-blank strings"
        )
    if (
        not isinstance(approver_user_values, list)
        or len(approver_user_values) != 2
        or not all(_is_positive_int(value) for value in approver_user_values)
    ):
        raise InvalidInputError(
            "G4 authority evidence approver_github_user_ids must contain two positive integers"
        )
    producer_github_user_id = _positive_int(state, "producer_github_user_id")
    evidence_pull_request_author_github_user_id = _positive_int(
        state, "evidence_pull_request_author_github_user_id"
    )
    verified_at = _datetime(state, "verified_at")
    return G4AuthorityEvidence(
        finding_id=finding_id,
        pull_request_number=pull_request_number,
        merged_head_revision=merged_head_revision,
        document_digest=document_digest,
        producer_github_user_id=producer_github_user_id,
        evidence_pull_request_author_github_user_id=(evidence_pull_request_author_github_user_id),
        approver_principal_ids=(approver_values[0], approver_values[1]),
        approver_github_user_ids=(approver_user_values[0], approver_user_values[1]),
        verified_at=verified_at,
    )


def _string(state: Mapping[str, object], field_name: str) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"G4 authority evidence {field_name} must be a non-blank string")
    return value


def _positive_int(state: Mapping[str, object], field_name: str) -> int:
    value = state.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidInputError(f"G4 authority evidence {field_name} must be a positive integer")
    return value


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _digest(state: Mapping[str, object], field_name: str) -> Digest:
    value = _string(state, field_name)
    algorithm, separator, hex_value = value.partition(":")
    if not separator:
        raise InvalidInputError(
            f"G4 authority evidence {field_name} must use the algorithm:hex form"
        )
    return Digest(algorithm=algorithm, hex_value=hex_value)


def _datetime(state: Mapping[str, object], field_name: str) -> datetime:
    value = _string(state, field_name)
    try:
        decoded = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidInputError(f"G4 authority evidence {field_name} must be ISO-8601") from error
    if decoded.tzinfo is None:
        raise InvalidInputError(f"G4 authority evidence {field_name} must include a timezone")
    return decoded


__all__ = ["G4AuthorityEvidence", "g4_authority_evidence_from_state"]
