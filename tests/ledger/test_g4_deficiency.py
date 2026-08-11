from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.g4_deficiency import G4DeficiencyFinding
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.g4_deficiency import (
    G4_DEFICIENCY_AGGREGATE_TYPE,
    record_g4_deficiency,
)
from enginery.ledger.service import LedgerService


def _finding() -> G4DeficiencyFinding:
    return G4DeficiencyFinding(
        finding_id="finding-1",
        deficiency="Validation command fails after generated dependency update.",
        cited_run_ids=("run-1", "run-2"),
        evidence_pull_request_number=42,
        producer_principal_id="operator-a",
        evidence_pull_request_author_login="author-a",
        recorded_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_record_finding_is_immutable(ledger_service: LedgerService) -> None:
    finding = _finding()

    record_g4_deficiency(ledger_service, finding=finding, correlation_id="record-finding-1")

    projection = ledger_service.read_projection(
        aggregate_type=G4_DEFICIENCY_AGGREGATE_TYPE, aggregate_id=finding.finding_id
    )
    assert projection is not None
    assert projection.state == finding.to_state()
    with pytest.raises(ExpectedVersionConflictError):
        record_g4_deficiency(ledger_service, finding=finding, correlation_id="record-finding-2")
