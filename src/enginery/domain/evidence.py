"""Evidence records with subject, freshness, and verifier provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.principal import AuthorityPrincipal


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One independently verifiable observation about a bound subject."""

    type: str
    schema_version: int
    producer: AuthorityPrincipal
    subject_revision: str | None
    observed_time: datetime
    validity_window_seconds: int
    result: EvidenceResult
    artifacts: tuple[Digest, ...] = field(default_factory=tuple)
    verifier_version: str = "1.0.0"
    subject_resource: str | None = None
    criterion_ids: tuple[str, ...] = field(default_factory=tuple)
    positive_implementation: bool = False
    implementation_diff_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.type.strip():
            raise InvalidInputError("evidence type must be non-blank")
        if self.schema_version < 1:
            raise InvalidInputError("evidence schema_version must be positive")
        if self.observed_time.tzinfo is None:
            raise InvalidInputError("evidence observed_time must be timezone-aware")
        if self.validity_window_seconds < 0:
            raise InvalidInputError("evidence validity_window_seconds cannot be negative")
        if self.subject_revision is None and self.subject_resource is None:
            raise InvalidInputError("evidence requires a revision or resource subject")
        if not self.verifier_version.strip():
            raise InvalidInputError("evidence verifier_version must be non-blank")
        if self.positive_implementation:
            if self.result is not EvidenceResult.PASS:
                raise InvalidInputError("positive implementation evidence must pass")
            if (
                self.implementation_diff_digest is None
                or not self.implementation_diff_digest.strip()
            ):
                raise InvalidInputError(
                    "positive implementation evidence requires a non-empty diff digest"
                )

    def is_stale(self, reference_time: datetime) -> bool:
        """Return whether the observation expires before ``reference_time``."""

        if reference_time.tzinfo is None:
            raise InvalidInputError("evidence reference_time must be timezone-aware")
        return reference_time > self.observed_time + timedelta(seconds=self.validity_window_seconds)

    def binds_subject(
        self,
        subject_revision: str | None,
        subject_resource: str | None = None,
    ) -> bool:
        """Return whether this evidence matches every supplied current subject."""

        return (subject_revision is None or self.subject_revision == subject_revision) and (
            subject_resource is None or self.subject_resource == subject_resource
        )


__all__ = ["EvidenceItem"]
