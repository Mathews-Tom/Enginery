"""Immutable, presentation-neutral review subjects; packets never grant authority."""

from __future__ import annotations

import json
from dataclasses import dataclass

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

MAX_PACKET_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReviewPacket:
    """One snapshot of durable truth, including explicitly missing evidence.

    Sections contain canonical JSON strings rather than mutable nested containers.
    Reconstructing a mapping for a consumer cannot mutate the reviewed subject.
    """

    run_id: str
    request_digest: str
    configuration_snapshot_digest: str
    base_revision: str
    head_revision: str
    diff: str
    evidence_receipt_digests: tuple[str, ...]
    ci_evidence_json: str
    findings_json: str
    sections: tuple[tuple[str, str], ...]
    protected_publication_digest: str = ""

    def __post_init__(self) -> None:
        if len({name for name, _ in self.sections}) != len(self.sections):
            raise InvalidInputError("review packet sections must have unique names")
        try:
            for value in (self.ci_evidence_json, self.findings_json):
                json.loads(value)
            for _, value in self.sections:
                json.loads(value)
            raw = json.dumps(self.to_state(), ensure_ascii=True, sort_keys=True).encode()
        except (ValueError, TypeError) as error:
            raise InvalidInputError("review packet contains invalid JSON") from error
        if len(raw) > MAX_PACKET_BYTES:
            raise InvalidInputError("review packet exceeds the 1 MiB limit")

    @property
    def diff_digest(self) -> str:
        return str(Digest.of_bytes(self.diff.encode()))

    def subject_fields(self) -> dict[str, object]:
        """Return every bound input for later action-specific decision verification."""
        ci: object = json.loads(self.ci_evidence_json)
        findings: object = json.loads(self.findings_json)
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "request_digest": self.request_digest,
            "configuration_snapshot_digest": self.configuration_snapshot_digest,
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
            "protected_publication_digest": self.protected_publication_digest,
            "diff_digest": self.diff_digest,
            "evidence_receipt_digests": list(self.evidence_receipt_digests),
            "ci_evidence_digest": str(Digest.of_json(ci)),
            "ci_evidence": ci,
            "findings": findings,
        }

    @property
    def subject_digest(self) -> str:
        return str(Digest.of_json(self.subject_fields()))

    @property
    def digest(self) -> Digest:
        return Digest.of_json(self.subject_fields())

    def to_state(self) -> dict[str, object]:
        return {
            **self.subject_fields(), "subject_digest": self.subject_digest, "diff": self.diff,
            "sections": {name: json.loads(value) for name, value in self.sections},
        }
