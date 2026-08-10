from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.digests import Digest
from enginery.domain.g4_authority_evidence import G4AuthorityEvidence
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.g4_authority_evidence import (
    G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE,
    record_g4_authority_evidence,
)
from enginery.ledger.service import LedgerService


def _evidence() -> G4AuthorityEvidence:
    return G4AuthorityEvidence(
        finding_id="finding-1",
        pull_request_number=42,
        merged_head_revision="a" * 40,
        document_digest=Digest.of_bytes(b"evidence"),
        approver_principal_ids=("one", "two"),
        verified_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_record_authority_evidence_is_immutable(ledger_service: LedgerService) -> None:
    evidence = _evidence()

    record_g4_authority_evidence(
        ledger_service, evidence=evidence, correlation_id="record-authority-evidence-1"
    )

    projection = ledger_service.read_projection(
        aggregate_type=G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE,
        aggregate_id=evidence.finding_id,
    )
    assert projection is not None
    assert projection.state == evidence.to_state()
    with pytest.raises(ExpectedVersionConflictError):
        record_g4_authority_evidence(
            ledger_service,
            evidence=evidence,
            correlation_id="record-authority-evidence-2",
        )
