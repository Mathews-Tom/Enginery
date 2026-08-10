"""Append-only persistence for recurring G4 deficiency findings."""

from __future__ import annotations

from enginery.domain.g4_deficiency import G4DeficiencyFinding
from enginery.ledger.events import AppendCommand, AppendResult, EventWrite
from enginery.ledger.service import LedgerService

G4_DEFICIENCY_AGGREGATE_TYPE = "g4_deficiency_finding"
G4_DEFICIENCY_EVENT_TYPE = "g4_deficiency_recorded"
G4_DEFICIENCY_SCHEMA_VERSION = 1


def record_g4_deficiency(
    ledger: LedgerService, *, finding: G4DeficiencyFinding, correlation_id: str
) -> AppendResult:
    """Record a new immutable finding; an existing ID is never overwritten."""
    return ledger.append(
        AppendCommand(
            correlation_id=correlation_id,
            events=(
                EventWrite(
                    aggregate_type=G4_DEFICIENCY_AGGREGATE_TYPE,
                    aggregate_id=finding.finding_id,
                    expected_version=0,
                    event_type=G4_DEFICIENCY_EVENT_TYPE,
                    schema_version=G4_DEFICIENCY_SCHEMA_VERSION,
                    payload=finding.to_state(),
                ),
            ),
        )
    )


__all__ = [
    "G4_DEFICIENCY_AGGREGATE_TYPE",
    "G4_DEFICIENCY_EVENT_TYPE",
    "G4_DEFICIENCY_SCHEMA_VERSION",
    "record_g4_deficiency",
]
