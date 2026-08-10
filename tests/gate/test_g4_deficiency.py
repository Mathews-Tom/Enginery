from __future__ import annotations

from datetime import UTC, datetime

from enginery.domain.digests import Digest
from enginery.domain.g4_deficiency import G4DeficiencyFinding
from enginery.evaluation.gate import ConditionStatus, G4Inputs, evaluate_g4
from enginery.evaluation.gate_floor import GateFloorConfig
from enginery.evaluation.outcomes import CompletenessReport


def _finding() -> G4DeficiencyFinding:
    return G4DeficiencyFinding(
        finding_id="finding-1",
        deficiency="Validation command fails after generated dependency update.",
        cited_run_ids=("run-1", "run-2"),
        evidence_pull_request_number=42,
        evidence_document_digest=Digest.of_bytes(b"evidence"),
        producer_principal_id="operator-a",
        evidence_pull_request_author_login="author-a",
        recorded_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_local_deficiency_record_never_passes_dual_authority_condition() -> None:
    report = evaluate_g4(
        floor=GateFloorConfig(1, (), None, None, None),
        inputs=G4Inputs(
            completed_run_count=2,
            completed_workflow_type_count=2,
            completed_risk_class_count=2,
            intervention_with_reason_count=0,
            completeness=CompletenessReport(1, 0, 0, 0, 1.0),
            repository_count=2,
            eligible_classified_completed_run_ids=("run-1", "run-2"),
            deficiency_findings=(_finding(),),
        ),
    )

    condition = next(
        item for item in report.conditions if item.id == "recurring_evidence_backed_deficiency"
    )
    assert condition.status is ConditionStatus.UNMEASURED
    assert condition.metrics["eligible_finding_count"] == 1
