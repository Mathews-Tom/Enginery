"""Append-only persistence for verified G4 authority evidence."""

from __future__ import annotations

from enginery.domain.g4_authority_evidence import G4AuthorityEvidence
from enginery.ledger.events import AppendCommand, AppendResult, EventWrite
from enginery.ledger.service import LedgerService

G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE = "g4_authority_evidence"
G4_AUTHORITY_EVIDENCE_EVENT_TYPE = "g4_authority_evidence_recorded"
G4_AUTHORITY_EVIDENCE_SCHEMA_VERSION = 1


def record_g4_authority_evidence(
    ledger: LedgerService, *, evidence: G4AuthorityEvidence, correlation_id: str
) -> AppendResult:
    """Record one verified evidence PR; an existing finding record is immutable."""
    return ledger.append(
        AppendCommand(
            correlation_id=correlation_id,
            events=(
                EventWrite(
                    aggregate_type=G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE,
                    aggregate_id=evidence.finding_id,
                    expected_version=0,
                    event_type=G4_AUTHORITY_EVIDENCE_EVENT_TYPE,
                    schema_version=G4_AUTHORITY_EVIDENCE_SCHEMA_VERSION,
                    payload=evidence.to_state(),
                ),
            ),
        )
    )


__all__ = [
    "G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE",
    "G4_AUTHORITY_EVIDENCE_EVENT_TYPE",
    "G4_AUTHORITY_EVIDENCE_SCHEMA_VERSION",
    "record_g4_authority_evidence",
]
