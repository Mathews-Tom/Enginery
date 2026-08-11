"""Immutable evidence reference for one recurring Stage 1 deficiency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class G4DeficiencyFinding:
    """A human-identified recurring deficiency awaiting external verification."""

    finding_id: str
    deficiency: str
    cited_run_ids: tuple[str, ...]
    evidence_pull_request_number: int
    producer_principal_id: str
    evidence_pull_request_author_login: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not self.finding_id.strip() or not self.deficiency.strip():
            raise InvalidInputError("G4 deficiency finding requires an id and description")
        if len(self.cited_run_ids) < 2 or len(set(self.cited_run_ids)) != len(self.cited_run_ids):
            raise InvalidInputError(
                "G4 deficiency finding requires at least two distinct cited runs"
            )
        if any(not run_id.strip() for run_id in self.cited_run_ids):
            raise InvalidInputError("G4 deficiency cited run ids must be non-blank")
        if self.evidence_pull_request_number < 1:
            raise InvalidInputError("G4 deficiency evidence pull request number must be positive")
        if (
            not self.producer_principal_id.strip()
            or not self.evidence_pull_request_author_login.strip()
        ):
            raise InvalidInputError("G4 deficiency finding requires producer and evidence author")

    @property
    def evidence_document(self) -> str:
        """Return the exact Markdown document that evidence-PR reviewers approve."""
        cited_runs = "\n".join(f"- `{run_id}`" for run_id in self.cited_run_ids)
        return (
            "# Enginery G4 recurring-deficiency evidence\n\n"
            f"Finding ID: `{self.finding_id}`\n\n"
            f"Deficiency: {self.deficiency}\n\n"
            "Cited verified classified runs:\n"
            f"{cited_runs}\n\n"
            f"Producer principal ID: `{self.producer_principal_id}`\n"
        )

    @property
    def evidence_document_digest(self) -> Digest:
        """Return the digest of the canonical reviewed evidence document."""
        return Digest.of_bytes(self.evidence_document.encode("utf-8"))

    def to_state(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "deficiency": self.deficiency,
            "cited_run_ids": list(self.cited_run_ids),
            "evidence_pull_request_number": self.evidence_pull_request_number,
            "evidence_document_digest": str(self.evidence_document_digest),
            "producer_principal_id": self.producer_principal_id,
            "evidence_pull_request_author_login": self.evidence_pull_request_author_login,
            "recorded_at": self.recorded_at.isoformat(),
        }


__all__ = ["G4DeficiencyFinding"]
