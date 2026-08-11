from __future__ import annotations

from datetime import UTC, datetime

from enginery.domain.digests import Digest
from enginery.domain.g4_authority_evidence import G4AuthorityEvidence
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
        producer_principal_id="operator-a",
        evidence_pull_request_author_login="author-a",
        recorded_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_finding_evidence_document_binds_its_cited_runs() -> None:
    finding = _finding()

    assert "Finding ID: `finding-1`" in finding.evidence_document
    assert "Deficiency: Validation command fails after generated dependency update." in (
        finding.evidence_document
    )
    assert "- `run-1`" in finding.evidence_document
    assert "- `run-2`" in finding.evidence_document
    assert finding.evidence_document_digest == Digest.of_bytes(
        finding.evidence_document.encode("utf-8")
    )


def _authority_evidence() -> G4AuthorityEvidence:
    finding = _finding()
    return G4AuthorityEvidence(
        finding_id=finding.finding_id,
        pull_request_number=finding.evidence_pull_request_number,
        merged_head_revision="a" * 40,
        document_digest=finding.evidence_document_digest,
        producer_github_user_id=100,
        evidence_pull_request_author_github_user_id=99,
        approver_principal_ids=("approver-one", "approver-two"),
        approver_github_user_ids=(101, 102),
        verified_at=datetime(2026, 8, 10, tzinfo=UTC),
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


def test_verified_current_authority_evidence_passes_deficiency_condition() -> None:
    report = evaluate_g4(
        floor=GateFloorConfig(
            schema_version=2,
            registered_principal_ids=("operator-a", "approver-one", "approver-two"),
            completed_run_volume_floor=None,
            intervention_volume_floor=None,
            outcome_completeness_floor=None,
            github_login_by_principal_id=(
                ("operator-a", "producer-login"),
                ("approver-one", "approver-one-login"),
                ("approver-two", "approver-two-login"),
            ),
            github_user_id_by_principal_id=(
                ("operator-a", 100),
                ("approver-one", 101),
                ("approver-two", 102),
            ),
        ),
        inputs=G4Inputs(
            completed_run_count=2,
            completed_workflow_type_count=2,
            completed_risk_class_count=2,
            intervention_with_reason_count=0,
            completeness=CompletenessReport(1, 0, 0, 0, 1.0),
            repository_count=2,
            eligible_classified_completed_run_ids=("run-1", "run-2"),
            deficiency_findings=(_finding(),),
            authority_evidence=(_authority_evidence(),),
        ),
    )
    condition = next(
        item for item in report.conditions if item.id == "recurring_evidence_backed_deficiency"
    )
    assert condition.status is ConditionStatus.PASS
    assert condition.metrics["authority_verified_finding_count"] == 1


def test_authority_evidence_fails_when_a_principal_id_is_remapped() -> None:
    report = evaluate_g4(
        floor=GateFloorConfig(
            schema_version=2,
            registered_principal_ids=("operator-a", "approver-one", "approver-two"),
            completed_run_volume_floor=None,
            intervention_volume_floor=None,
            outcome_completeness_floor=None,
            github_user_id_by_principal_id=(
                ("operator-a", 100),
                ("approver-one", 1101),
                ("approver-two", 102),
            ),
        ),
        inputs=G4Inputs(
            completed_run_count=2,
            completed_workflow_type_count=2,
            completed_risk_class_count=2,
            intervention_with_reason_count=0,
            completeness=CompletenessReport(1, 0, 0, 0, 1.0),
            repository_count=2,
            eligible_classified_completed_run_ids=("run-1", "run-2"),
            deficiency_findings=(_finding(),),
            authority_evidence=(_authority_evidence(),),
        ),
    )

    condition = next(
        item for item in report.conditions if item.id == "recurring_evidence_backed_deficiency"
    )
    assert condition.status is ConditionStatus.UNMEASURED
