"""Falsifiable merge-ready and released terminal-state contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.evidence import EvidenceItem
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.policy_decision import (
    ApprovalAttestation,
    PolicyAction,
    PolicyDecision,
    PolicyResult,
)
from enginery.evidence.evaluator import EvidenceContract, EvidenceEvaluation, EvidenceEvaluator


class TerminalContractError(InvalidInputError):
    """Raised when a terminal contract is structurally invalid."""


@dataclass(frozen=True, slots=True)
class TerminalEvaluation:
    """An auditable terminal decision; only ``PASS`` can claim completion."""

    result: EvidenceResult
    reasons: tuple[str, ...]
    evidence_evaluation: EvidenceEvaluation | None = None

    @property
    def passes(self) -> bool:
        return self.result is EvidenceResult.PASS


@dataclass(frozen=True, slots=True)
class MergeReadySubject:
    """One observed pull-request subject used by the double-read verifier."""

    work_revision: str
    base_sha: str
    head_sha: str
    has_conflicts: bool
    metadata_work_item_linked: bool
    metadata_evidence_bundle_linked: bool

    def __post_init__(self) -> None:
        if not self.work_revision or not self.base_sha or not self.head_sha:
            raise TerminalContractError(
                "merge-ready subjects require work, base, and head revisions"
            )


@dataclass(frozen=True, slots=True)
class NonApplicabilityDecision:
    """An independently approved, criterion-scoped non-applicability decision."""

    criterion_id: str
    target_resource: str
    schema_digest: Digest
    approval: ApprovalAttestation

    def is_current(self, reference_time: datetime) -> bool:
        return (
            self.approval.action is PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT
            and self.target_resource == self.criterion_id
            and self.approval.schema_digest == self.schema_digest
            and self.approval.binds_input("target_resource", self.target_resource)
            and self.approval.has_independent_human_approval()
            and self.approval.is_current(reference_time)
        )


@dataclass(frozen=True, slots=True)
class MergeReadyContext:
    """All facts required to establish a current, non-empty merge-ready claim."""

    first_subject: MergeReadySubject
    second_subject: MergeReadySubject
    expected_diff_digest: str | None
    acceptance_criteria: tuple[str, ...]
    evidence_contract: EvidenceContract
    evidence_items: tuple[EvidenceItem, ...]
    non_applicability: tuple[NonApplicabilityDecision, ...]
    terminal_policy_decision: PolicyDecision

    def __post_init__(self) -> None:
        if len(set(self.acceptance_criteria)) != len(self.acceptance_criteria):
            raise TerminalContractError("merge-ready acceptance criteria must be unique")


class MergeReadyVerifier:
    """Verify merge readiness without allowing stale or no-op claims."""

    def verify(self, context: MergeReadyContext, reference_time: datetime) -> TerminalEvaluation:
        if context.first_subject != context.second_subject:
            return TerminalEvaluation(
                EvidenceResult.INDETERMINATE,
                ("pull-request subject changed during double-read verification",),
            )
        subject = context.second_subject
        if subject.has_conflicts:
            return TerminalEvaluation(EvidenceResult.FAIL, ("unresolved conflicts remain",))
        if not subject.metadata_work_item_linked or not subject.metadata_evidence_bundle_linked:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("pull-request metadata must link work item and evidence bundle",),
            )
        if context.expected_diff_digest is None or not context.expected_diff_digest.strip():
            return TerminalEvaluation(EvidenceResult.FAIL, ("empty diff cannot be merge-ready",))
        if context.terminal_policy_decision.result is not PolicyResult.ALLOW:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("policy does not permit the terminal transition",),
            )
        evidence_evaluation = EvidenceEvaluator().evaluate(
            context.evidence_contract,
            context.evidence_items,
            reference_time,
        )
        if evidence_evaluation.result is not EvidenceResult.PASS:
            return TerminalEvaluation(
                evidence_evaluation.result,
                ("required validation evidence is incomplete, stale, or failing",),
                evidence_evaluation,
            )
        decisions = {decision.criterion_id: decision for decision in context.non_applicability}
        missing_criteria = [
            criterion
            for criterion in context.acceptance_criteria
            if not self._has_current_implementation_evidence(
                context.evidence_items,
                criterion,
                subject.head_sha,
                reference_time,
            )
            and not (
                (decision := decisions.get(criterion)) is not None
                and decision.is_current(reference_time)
            )
        ]
        if missing_criteria:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                (f"acceptance criteria lack current evidence: {', '.join(missing_criteria)}",),
                evidence_evaluation,
            )
        has_positive_implementation = any(
            item.positive_implementation
            and item.result is EvidenceResult.PASS
            and item.subject_revision == subject.head_sha
            and item.implementation_diff_digest == context.expected_diff_digest
            and not item.is_stale(reference_time)
            for item in context.evidence_items
        )
        if not has_positive_implementation:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("no positive implementation evidence is tied to the expected non-empty diff",),
                evidence_evaluation,
            )
        return TerminalEvaluation(EvidenceResult.PASS, (), evidence_evaluation)

    @staticmethod
    def _has_current_implementation_evidence(
        evidence_items: tuple[EvidenceItem, ...],
        criterion: str,
        head_sha: str,
        reference_time: datetime,
    ) -> bool:
        return any(
            criterion in item.criterion_ids
            and item.result is EvidenceResult.PASS
            and item.subject_revision == head_sha
            and not item.is_stale(reference_time)
            for item in evidence_items
        )


@dataclass(frozen=True, slots=True)
class ReleasedSubject:
    """One double-read release subject."""

    commit_sha: str
    tag_name: str
    destination_revision: str

    def __post_init__(self) -> None:
        if not self.commit_sha or not self.tag_name or not self.destination_revision:
            raise TerminalContractError("released subjects require commit, tag, and destination")


@dataclass(frozen=True, slots=True)
class ReleaseRemediationDecision:
    """An independently approved procedure for irreversible destinations."""

    override_scope: tuple[str, ...]
    schema_digest: Digest
    approval: ApprovalAttestation

    def is_current(self, reference_time: datetime) -> bool:
        return (
            self.approval.action is PolicyAction.POLICY_OVERRIDE
            and "irreversible_publication_remediation" in self.override_scope
            and self.approval.schema_digest == self.schema_digest
            and self.approval.binds_input("override_scope", sorted(self.override_scope))
            and self.approval.has_independent_human_approval()
            and self.approval.is_current(reference_time)
        )


@dataclass(frozen=True, slots=True)
class ReleasedContext:
    """All facts required to establish a released terminal claim."""

    first_subject: ReleasedSubject
    second_subject: ReleasedSubject
    constituent_work_merged: bool
    version_matches_policy: bool
    changelog_matches_policy: bool
    smoke_contract: EvidenceContract
    evidence_items: tuple[EvidenceItem, ...]
    tag_references_commit: bool
    artifacts_reference_commit: bool
    publication_verified: bool
    rollback_capability_tested: bool
    irreversible_remediation: ReleaseRemediationDecision | None
    state_reconciled: bool
    terminal_policy_decision: PolicyDecision


class ReleasedVerifier:
    """Verify destination-observed release completion."""

    def verify(self, context: ReleasedContext, reference_time: datetime) -> TerminalEvaluation:
        if context.first_subject != context.second_subject:
            return TerminalEvaluation(
                EvidenceResult.INDETERMINATE,
                ("release subject changed during double-read verification",),
            )
        if context.terminal_policy_decision.result is not PolicyResult.ALLOW:
            return TerminalEvaluation(EvidenceResult.FAIL, ("policy does not permit release",))
        if not context.constituent_work_merged:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("required constituent work is not merged",),
            )
        if not context.version_matches_policy or not context.changelog_matches_policy:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("version or changelog does not match release policy",),
            )
        evidence_evaluation = EvidenceEvaluator().evaluate(
            context.smoke_contract,
            context.evidence_items,
            reference_time,
        )
        if evidence_evaluation.result is not EvidenceResult.PASS:
            return TerminalEvaluation(
                evidence_evaluation.result,
                ("release smoke evidence is incomplete, stale, or failing",),
                evidence_evaluation,
            )
        if not context.tag_references_commit or not context.artifacts_reference_commit:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("tag and release artifacts must reference the intended commit",),
                evidence_evaluation,
            )
        if not context.publication_verified:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("publication must be verified from the destination",),
                evidence_evaluation,
            )
        if not context.rollback_capability_tested and not (
            context.irreversible_remediation
            and context.irreversible_remediation.is_current(reference_time)
        ):
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("release requires tested rollback or an independently approved remediation",),
                evidence_evaluation,
            )
        if not context.state_reconciled:
            return TerminalEvaluation(
                EvidenceResult.FAIL,
                ("external work state and local evidence are not reconciled",),
                evidence_evaluation,
            )
        return TerminalEvaluation(EvidenceResult.PASS, (), evidence_evaluation)


__all__ = [
    "MergeReadyContext",
    "MergeReadySubject",
    "MergeReadyVerifier",
    "NonApplicabilityDecision",
    "ReleaseRemediationDecision",
    "ReleasedContext",
    "ReleasedSubject",
    "ReleasedVerifier",
    "TerminalContractError",
    "TerminalEvaluation",
]
