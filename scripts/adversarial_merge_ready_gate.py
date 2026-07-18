"""Execute generated merge-ready bypass attempts against terminal contracts."""

from __future__ import annotations

import random
import secrets
import string
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
)
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.schemas import ApprovalSchema


def _token(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(20))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _allowed_policy(now: datetime) -> PolicyDecision:
    return PolicyDecision(
        id=PolicyDecisionId("generated-policy"),
        action=PolicyAction.PULL_REQUEST_OPEN,
        normalized_inputs={},
        policy_rule_id="generated",
        policy_version="generated",
        result=PolicyResult.ALLOW,
        rationale="generated authority boundary",
        input_digest=Digest.of_json({}),
        decided_at=now,
    )


def _context(token: str, now: datetime) -> MergeReadyContext:
    subject = MergeReadySubject(
        work_revision=f"work-{token}",
        base_sha=f"base-{token}",
        head_sha=f"head-{token}",
        has_conflicts=False,
        metadata_work_item_linked=True,
        metadata_evidence_bundle_linked=True,
    )
    criterion = f"criterion-{token}"
    diff_digest = f"diff-{token}"
    producer = AuthorityPrincipal(
        f"run-{token}",
        PrincipalType.AGENT,
        "worker",
        "generated-fixture",
    )
    ci = EvidenceItem(
        type="ci",
        schema_version=1,
        producer=producer,
        subject_revision=subject.head_sha,
        observed_time=now,
        validity_window_seconds=3600,
        result=EvidenceResult.PASS,
    )
    implementation = EvidenceItem(
        type="implementation",
        schema_version=1,
        producer=producer,
        subject_revision=subject.head_sha,
        observed_time=now,
        validity_window_seconds=3600,
        result=EvidenceResult.PASS,
        criterion_ids=(criterion,),
        positive_implementation=True,
        implementation_diff_digest=diff_digest,
    )
    return MergeReadyContext(
        first_subject=subject,
        second_subject=subject,
        expected_diff_digest=diff_digest,
        acceptance_criteria=(criterion,),
        evidence_contract=EvidenceContract((EvidenceRequirement("ci", "ci", subject.head_sha),)),
        evidence_items=(ci, implementation),
        non_applicability=(),
        terminal_policy_decision=_allowed_policy(now),
    )


def run_gate() -> None:
    seed = secrets.randbits(64)
    rng = random.Random(seed)
    verifier = MergeReadyVerifier()
    cases = 0
    for _ in range(32):
        now = datetime.now(UTC)
        context = _context(_token(rng), now)
        _assert(verifier.verify(context, now).result is EvidenceResult.PASS, "valid claim failed")

        _assert(
            verifier.verify(replace(context, expected_diff_digest=None), now).result
            is EvidenceResult.FAIL,
            "empty implementation claim passed",
        )

        stale_ci = replace(
            context.evidence_items[0],
            observed_time=now - timedelta(seconds=61),
            validity_window_seconds=60,
        )
        _assert(
            verifier.verify(
                replace(context, evidence_items=(stale_ci, context.evidence_items[1])),
                now,
            ).result
            is EvidenceResult.FAIL,
            "stale evidence claim passed",
        )

        wrong_subject_ci = replace(
            context.evidence_items[0],
            subject_revision=f"wrong-{_token(rng)}",
        )
        _assert(
            verifier.verify(
                replace(context, evidence_items=(wrong_subject_ci, context.evidence_items[1])),
                now,
            ).result
            is EvidenceResult.FAIL,
            "wrong-subject evidence claim passed",
        )

        indeterminate_ci = replace(context.evidence_items[0], result=EvidenceResult.INDETERMINATE)
        _assert(
            verifier.verify(
                replace(context, evidence_items=(indeterminate_ci, context.evidence_items[1])),
                now,
            ).result
            is EvidenceResult.INDETERMINATE,
            "indeterminate evidence claim passed",
        )

        changed_subject = replace(context.second_subject, head_sha=f"changed-{_token(rng)}")
        _assert(
            verifier.verify(replace(context, second_subject=changed_subject), now).result
            is EvidenceResult.INDETERMINATE,
            "subject mutation between reads passed",
        )

        criterion = context.acceptance_criteria[0]
        producer = context.evidence_items[0].producer
        approver = AuthorityPrincipal(
            f"operator-{_token(rng)}",
            PrincipalType.HUMAN,
            "operator",
            "generated-fixture",
        )
        schema = ApprovalSchema(
            action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
            requesting_principal_id=producer.id,
            producer_principal_ids=(producer.id,),
            target_resource=criterion,
        )
        attestation = (
            ApprovalRegistry((approver,))
            .record_approval(
                schema,
                (approver,),
                decided_at=now,
            )
            .attestation()
        )
        non_applicability = NonApplicabilityDecision(
            criterion_id=criterion,
            target_resource=criterion,
            schema_digest=attestation.schema_digest,
            approval=attestation,
        )
        _assert(
            verifier.verify(
                replace(
                    context,
                    evidence_items=(context.evidence_items[0],),
                    non_applicability=(non_applicability,),
                ),
                now,
            ).result
            is EvidenceResult.FAIL,
            "all-non-applicable implementation claim passed",
        )

        self_approver = AuthorityPrincipal(
            f"self-{_token(rng)}",
            PrincipalType.HUMAN,
            "operator",
            "generated-fixture",
        )
        self_criterion = f"unproven-{_token(rng)}"
        self_schema = ApprovalSchema(
            action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
            requesting_principal_id=self_approver.id,
            producer_principal_ids=(self_approver.id,),
            target_resource=self_criterion,
        )
        self_attestation = ApprovalAttestation(
            action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
            schema_digest=self_schema.digest(),
            normalized_inputs=self_schema.canonical_inputs(),
            approvers=(self_approver,),
            approved=True,
        )
        self_non_applicability = NonApplicabilityDecision(
            criterion_id=self_criterion,
            target_resource=self_criterion,
            schema_digest=self_attestation.schema_digest,
            approval=self_attestation,
        )
        _assert(
            verifier.verify(
                replace(
                    context,
                    acceptance_criteria=(criterion, self_criterion),
                    non_applicability=(self_non_applicability,),
                ),
                now,
            ).result
            is EvidenceResult.FAIL,
            "self-approved non-applicability claim passed",
        )
        cases += 1
    print(f"PASS merge-ready adversarial cases={cases} seed={seed}")


if __name__ == "__main__":
    run_gate()
