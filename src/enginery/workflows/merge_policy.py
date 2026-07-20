"""Root-to-leaf merge execution for one plan's ``Stack``.

Reads durable ``Stack``/``StackSlice`` evidence (built and persisted by
``StackCoordinator``), re-verifies the next mergeable slice's pull request
against exact-head evidence immediately before merging, and records the
merge durably through ``StackCoordinator.mark_merged``. Root-to-leaf
ordering is enforced structurally by ``Stack.mark_merged`` itself, not by
caller discipline.

This module never merges more than one slice per call: each merge is its
own independently policy-evaluated, independently evidenced action,
matching the merge-ready contract's "policy approval" and "current
evidence" requirements per slice rather than for the stack as a whole.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from enginery.application.work_ports import PullRequestPort
from enginery.domain.enums import RiskClass
from enginery.domain.errors import (
    InternalInvariantViolationError,
    MissingPrerequisiteError,
    PolicyDenialError,
)
from enginery.domain.ids import OperationId, PlanMilestoneId, StackId
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.stack import Stack
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema
from enginery.workflows.pull_request import (
    PullRequestOutcome,
    PullRequestRequirements,
    evaluate_pull_request,
)


@dataclass(frozen=True, slots=True)
class MergeAttemptOutcome:
    """One merge attempt's result: either a newly merged slice, or why none merged."""

    stack: Stack
    milestone_id: PlanMilestoneId | None
    merged: bool
    detail: str


@dataclass(frozen=True, slots=True)
class MergePolicyService:
    """Merge one plan's stack root-to-leaf under the merge-ready contract."""

    stacks: StackCoordinator
    pull_requests: PullRequestPort
    policy: PolicyEvaluator

    def merge_next(
        self,
        stack_id: StackId,
        *,
        pull_request_number: int,
        required_checks: tuple[str, ...],
        require_approved_review: bool,
        risk_class: RiskClass,
        requesting_principal_id: str,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> MergeAttemptOutcome:
        """Merge the single next eligible slice, or report why none merged.

        The caller supplies ``pull_request_number`` for the slice it
        expects to be next -- the workflow layer already owns the
        milestone-to-pull-request correlation from the pull-request-open
        step, so this service does not re-derive it. A mismatch between
        the supplied pull request and the actually-next slice's branch
        surfaces as a stale/blocked outcome rather than a silent merge of
        the wrong pull request.
        """
        stack = self.stacks.read(stack_id)
        if stack is None:
            raise MissingPrerequisiteError(
                "stack does not exist", details={"stack_id": str(stack_id)}
            )
        milestone_id = stack.next_mergeable()
        if milestone_id is None:
            return MergeAttemptOutcome(
                stack=stack,
                milestone_id=None,
                merged=False,
                detail="no slice is currently eligible for a root-to-leaf merge",
            )
        slice_ = stack.slice(milestone_id)
        if slice_.head_revision is None:
            raise InternalInvariantViolationError(
                "a merge_ready slice must carry a bound head_revision",
                details={"milestone_id": str(milestone_id)},
            )
        expected_head_revision = slice_.head_revision

        schema = ApprovalSchema(
            action=PolicyAction.PULL_REQUEST_MERGE,
            risk_class=risk_class,
            target_resource=slice_.branch_ref,
            diff_or_artifact_digest=str(slice_.ci_evidence_digest),
            requesting_principal_id=requesting_principal_id,
        )
        decision = self.policy.evaluate(schema)
        if decision.result is not PolicyResult.ALLOW:
            raise PolicyDenialError(
                f"policy does not permit merging {milestone_id}",
                details={
                    "policy_rule_id": decision.policy_rule_id,
                    "result": decision.result.value,
                },
            )

        evidence = self.pull_requests.evidence(pull_request_number)
        outcome = evaluate_pull_request(
            evidence,
            PullRequestRequirements(
                expected_head_revision=expected_head_revision,
                required_checks=required_checks,
                require_approved_review=require_approved_review,
            ),
        )
        if outcome is not PullRequestOutcome.MERGE_READY:
            return MergeAttemptOutcome(
                stack=stack,
                milestone_id=milestone_id,
                merged=False,
                detail=f"pull request is not currently merge-ready: {outcome.value}",
            )

        operation_id = _merge_operation_id(stack_id, milestone_id)
        merged_snapshot = self.pull_requests.merge(
            pull_request_number,
            expected_head_revision=expected_head_revision,
            operation_id=operation_id,
        )
        updated_stack = self.stacks.mark_merged(
            stack_id, milestone_id, now=now, heartbeat_window=heartbeat_window
        )
        return MergeAttemptOutcome(
            stack=updated_stack,
            milestone_id=milestone_id,
            merged=True,
            detail=f"merged at {merged_snapshot.head_revision}",
        )


def _merge_operation_id(stack_id: StackId, milestone_id: PlanMilestoneId) -> OperationId:
    """A stable operation identity for one slice's merge, reused across retries.

    Scoped to the stack and milestone rather than a workflow run, since a
    merge is not itself a manifest-node side effect with a ``RunId`` --
    ``OperationId.derive`` does not fit. Retrying the same slice's merge
    always reuses this exact ID, matching ``OperationId``'s own contract.
    """
    payload = "\x1f".join(("stack-merge", str(stack_id), str(milestone_id)))
    return OperationId(value=hashlib.sha256(payload.encode("utf-8")).hexdigest())


__all__ = ["MergeAttemptOutcome", "MergePolicyService"]
