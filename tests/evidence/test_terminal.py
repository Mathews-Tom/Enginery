from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from enginery.domain.digests import Digest
from enginery.domain.evidence import EvidenceItem
from enginery.domain.ids import PolicyDecisionId
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.policy_decision import (
    ApprovalAttestation,
    PolicyAction,
    PolicyDecision,
    PolicyResult,
)
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.evidence.evaluator import EvidenceContract, EvidenceRequirement
from enginery.evidence.terminal import (
    MergeReadyContext,
    MergeReadySubject,
    MergeReadyVerifier,
    NonApplicabilityDecision,
    ReleasedContext,
    ReleasedSubject,
    ReleasedVerifier,
)
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.schemas import ApprovalSchema


def _agent() -> AuthorityPrincipal:
    return AuthorityPrincipal("agent-1", PrincipalType.AGENT, "worker", "fixture")


def _approved_non_applicability(
    now: datetime,
    criterion_id: str,
) -> ApprovalAttestation:
    producer = _agent()
    approver = AuthorityPrincipal("operator-1", PrincipalType.HUMAN, "operator", "fixture")
    schema = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=producer.id,
        producer_principal_ids=(producer.id,),
        target_resource=criterion_id,
    )
    registry = ApprovalRegistry((approver,))
    return registry.record_approval(schema, (approver,), decided_at=now).attestation()


def _allowed_policy(action: PolicyAction) -> PolicyDecision:
    return PolicyDecision(
        id=PolicyDecisionId("policy-1"),
        action=action,
        normalized_inputs={},
        policy_rule_id="test",
        policy_version="1",
        result=PolicyResult.ALLOW,
        rationale="test authority",
        input_digest=Digest.of_json({}),
        decided_at=datetime.now(UTC),
    )


def _merge_subject(head: str = "head-1") -> MergeReadySubject:
    return MergeReadySubject(
        work_revision="work-1",
        base_sha="base-1",
        head_sha=head,
        has_conflicts=False,
        metadata_work_item_linked=True,
        metadata_evidence_bundle_linked=True,
    )


def _merge_evidence(
    now: datetime,
    *,
    ci_result: EvidenceResult = EvidenceResult.PASS,
) -> tuple[EvidenceItem, ...]:
    return (
        EvidenceItem(
            type="ci",
            schema_version=1,
            producer=_agent(),
            subject_revision="head-1",
            observed_time=now,
            validity_window_seconds=3600,
            result=ci_result,
        ),
        EvidenceItem(
            type="implementation",
            schema_version=1,
            producer=_agent(),
            subject_revision="head-1",
            observed_time=now,
            validity_window_seconds=3600,
            result=EvidenceResult.PASS,
            criterion_ids=("criterion-1",),
            positive_implementation=True,
            implementation_diff_digest="diff-1",
        ),
    )


def _merge_context(
    now: datetime,
    *,
    first_subject: MergeReadySubject | None = None,
    second_subject: MergeReadySubject | None = None,
    expected_diff_digest: str | None = "diff-1",
    evidence_items: tuple[EvidenceItem, ...] | None = None,
) -> MergeReadyContext:
    subject = _merge_subject()
    return MergeReadyContext(
        first_subject=first_subject or subject,
        second_subject=second_subject or subject,
        expected_diff_digest=expected_diff_digest,
        acceptance_criteria=("criterion-1",),
        evidence_contract=EvidenceContract((EvidenceRequirement("ci", "ci", "head-1"),)),
        evidence_items=evidence_items or _merge_evidence(now),
        non_applicability=(),
        terminal_policy_decision=_allowed_policy(PolicyAction.PULL_REQUEST_OPEN),
    )


def test_merge_ready_requires_current_positive_implementation_evidence() -> None:
    now = datetime.now(UTC)

    evaluation = MergeReadyVerifier().verify(_merge_context(now), now)

    assert evaluation.result is EvidenceResult.PASS


def test_merge_ready_rejects_empty_diff_claim() -> None:
    now = datetime.now(UTC)

    evaluation = MergeReadyVerifier().verify(_merge_context(now, expected_diff_digest=None), now)

    assert evaluation.result is EvidenceResult.FAIL
    assert "empty diff" in evaluation.reasons[0]


def test_all_non_applicable_claims_cannot_replace_positive_implementation() -> None:
    now = datetime.now(UTC)
    attestation = _approved_non_applicability(now, "criterion-1")
    non_applicability = NonApplicabilityDecision(
        criterion_id="criterion-1",
        target_resource="criterion-1",
        schema_digest=attestation.schema_digest,
        approval=attestation,
    )
    ci_only = (_merge_evidence(now)[0],)
    context = _merge_context(now, evidence_items=ci_only)
    context = MergeReadyContext(
        first_subject=context.first_subject,
        second_subject=context.second_subject,
        expected_diff_digest=context.expected_diff_digest,
        acceptance_criteria=context.acceptance_criteria,
        evidence_contract=context.evidence_contract,
        evidence_items=context.evidence_items,
        non_applicability=(non_applicability,),
        terminal_policy_decision=context.terminal_policy_decision,
    )

    evaluation = MergeReadyVerifier().verify(context, now)

    assert evaluation.result is EvidenceResult.FAIL
    assert "positive implementation evidence" in evaluation.reasons[0]


def test_merge_ready_rejects_self_approved_non_applicability() -> None:
    now = datetime.now(UTC)
    operator = AuthorityPrincipal("operator-1", PrincipalType.HUMAN, "operator", "fixture")
    schema = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=operator.id,
        producer_principal_ids=(operator.id,),
        target_resource="criterion-2",
    )
    attestation = ApprovalAttestation(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        schema_digest=schema.digest(),
        normalized_inputs=schema.canonical_inputs(),
        approvers=(operator,),
        approved=True,
    )
    non_applicability = NonApplicabilityDecision(
        criterion_id="criterion-2",
        target_resource="criterion-2",
        schema_digest=attestation.schema_digest,
        approval=attestation,
    )
    context = replace(
        _merge_context(now),
        acceptance_criteria=("criterion-1", "criterion-2"),
        non_applicability=(non_applicability,),
    )

    evaluation = MergeReadyVerifier().verify(context, now)

    assert evaluation.result is EvidenceResult.FAIL
    assert "criterion-2" in evaluation.reasons[0]


def test_hard_required_evidence_cannot_be_waived_by_non_applicability() -> None:
    now = datetime.now(UTC)
    attestation = _approved_non_applicability(now, "criterion-1")
    decision = NonApplicabilityDecision(
        criterion_id="criterion-1",
        target_resource="criterion-1",
        schema_digest=attestation.schema_digest,
        approval=attestation,
    )
    context = replace(
        _merge_context(now),
        evidence_items=(),
        non_applicability=(decision,),
    )

    evaluation = MergeReadyVerifier().verify(context, now)

    assert evaluation.result is EvidenceResult.INDETERMINATE
    assert evaluation.evidence_evaluation is not None
    assert evaluation.evidence_evaluation.missing == ("ci",)


def test_merge_ready_rejects_stale_current_head_evidence() -> None:
    now = datetime.now(UTC)
    stale_ci = EvidenceItem(
        type="ci",
        schema_version=1,
        producer=_agent(),
        subject_revision="head-1",
        observed_time=now - timedelta(seconds=61),
        validity_window_seconds=60,
        result=EvidenceResult.PASS,
    )
    evidence = (stale_ci, _merge_evidence(now)[1])

    evaluation = MergeReadyVerifier().verify(_merge_context(now, evidence_items=evidence), now)

    assert evaluation.result is EvidenceResult.FAIL
    assert evaluation.evidence_evaluation is not None
    assert evaluation.evidence_evaluation.stale == ("ci",)


def test_merge_ready_indeterminate_evidence_blocks_claim() -> None:
    now = datetime.now(UTC)

    evaluation = MergeReadyVerifier().verify(
        _merge_context(
            now,
            evidence_items=_merge_evidence(now, ci_result=EvidenceResult.INDETERMINATE),
        ),
        now,
    )

    assert evaluation.result is EvidenceResult.INDETERMINATE


def test_merge_ready_double_read_subject_change_is_indeterminate() -> None:
    now = datetime.now(UTC)

    evaluation = MergeReadyVerifier().verify(
        _merge_context(now, second_subject=_merge_subject("head-2")),
        now,
    )

    assert evaluation.result is EvidenceResult.INDETERMINATE
    assert "double-read" in evaluation.reasons[0]


def _released_context(now: datetime, *, publication_verified: bool = True) -> ReleasedContext:
    subject = ReleasedSubject("commit-1", "v0.1.0", "commit-1")
    smoke = EvidenceItem(
        type="smoke",
        schema_version=1,
        producer=_agent(),
        subject_revision="commit-1",
        observed_time=now,
        validity_window_seconds=3600,
        result=EvidenceResult.PASS,
    )
    return ReleasedContext(
        first_subject=subject,
        second_subject=subject,
        constituent_work_merged=True,
        version_matches_policy=True,
        changelog_matches_policy=True,
        smoke_contract=EvidenceContract((EvidenceRequirement("smoke", "smoke", "commit-1"),)),
        evidence_items=(smoke,),
        tag_references_commit=True,
        artifacts_reference_commit=True,
        publication_verified=publication_verified,
        rollback_capability_tested=True,
        irreversible_remediation=None,
        state_reconciled=True,
        terminal_policy_decision=_allowed_policy(PolicyAction.RELEASE_PUBLISH),
    )


def test_released_contract_requires_destination_verified_release() -> None:
    now = datetime.now(UTC)

    assert ReleasedVerifier().verify(_released_context(now), now).result is EvidenceResult.PASS
    assert (
        ReleasedVerifier().verify(_released_context(now, publication_verified=False), now).result
        is EvidenceResult.FAIL
    )
