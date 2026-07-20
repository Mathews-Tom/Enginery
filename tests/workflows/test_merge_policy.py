"""Tests for enginery.workflows.merge_policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.application.adapter_types import AdapterStatus
from enginery.application.work_ports import (
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestRequest,
    PullRequestSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass
from enginery.domain.errors import MissingPrerequisiteError, PolicyDenialError
from enginery.domain.ids import OperationId, PlanId, PlanMilestoneId, StackId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.stack import StackSliceState
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService
from enginery.policy.evaluator import PolicyEvaluator, PolicyRule
from enginery.workflows.merge_policy import MergePolicyService

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=30)
_HEAD = "m1-rev1"
_MILESTONES = (
    (PlanMilestoneId("m1"), "feature/m1"),
    (PlanMilestoneId("m2"), "feature/m2"),
)


class FakePullRequests:
    """A minimal structural PullRequestPort double for merge tests."""

    def __init__(self, evidence: PullRequestEvidence, snapshot: PullRequestSnapshot) -> None:
        self._evidence = evidence
        self._snapshot = snapshot
        self.merge_calls: list[tuple[int, str]] = []

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by these tests
        raise NotImplementedError

    def create_or_update(
        self, request: PullRequestRequest
    ) -> PullRequestSnapshot:  # pragma: no cover - unused
        raise NotImplementedError

    def get(self, number: int) -> PullRequestSnapshot:  # pragma: no cover - unused
        raise NotImplementedError

    def evidence(self, number: int) -> PullRequestEvidence:
        return self._evidence

    def merge(
        self,
        number: int,
        *,
        expected_head_revision: str,
        operation_id: OperationId,
        merge_method: str = "merge",
    ) -> PullRequestSnapshot:
        self.merge_calls.append((number, expected_head_revision))
        return self._snapshot

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return ReconciliationResult.NOT_FOUND


def _stack_coordinator(ledger: LedgerService) -> StackCoordinator:
    return StackCoordinator(ledger, CoordinatorRuntime(ledger, owner="test-coordinator"))


def _merge_ready_stack(coordinator: StackCoordinator, *, stack_id: StackId) -> None:
    coordinator.start(
        stack_id=stack_id,
        plan_id=PlanId("plan-1"),
        base_ref="main",
        ordered_milestones=_MILESTONES,
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    coordinator.reconcile_after_publish(
        stack_id, PlanMilestoneId("m1"), head_revision=_HEAD, now=_NOW, heartbeat_window=_HEARTBEAT
    )
    coordinator.mark_merge_ready(
        stack_id,
        PlanMilestoneId("m1"),
        head_revision=_HEAD,
        ci_evidence_digest=Digest.of_bytes(b"ci-passed"),
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )


def _evidence(*, head: str = _HEAD, mergeable: bool | None = True) -> PullRequestEvidence:
    snapshot = PullRequestSnapshot(
        number=11,
        url="https://github.com/Mathews-Tom/enginery-provider-smoke/pull/11",
        state="open",
        head_branch="feature/m1",
        head_revision=head,
        base_branch="main",
        base_revision="b" * 40,
    )
    return PullRequestEvidence(
        pull_request=snapshot,
        reviews=(),
        checks=(
            PullRequestCheck(
                name="ci", status="completed", conclusion="success", head_revision=head
            ),
        ),
        mergeable=mergeable,
    )


def _merged_snapshot(*, head: str = _HEAD) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=11,
        url="https://github.com/Mathews-Tom/enginery-provider-smoke/pull/11",
        state="closed",
        head_branch="feature/m1",
        head_revision=head,
        base_branch="main",
        base_revision="b" * 40,
        merged=True,
    )


def _allow_evaluator() -> PolicyEvaluator:
    return PolicyEvaluator(
        policy_version="1.0.0",
        rules=(
            PolicyRule(
                id="allow_merge",
                action=PolicyAction.PULL_REQUEST_MERGE,
                result=PolicyResult.ALLOW,
                rationale="test",
                risk_classes=frozenset({RiskClass.LOW}),
            ),
        ),
    )


def test_merges_the_next_eligible_slice_and_records_it_durably(
    ledger_service: LedgerService,
) -> None:
    coordinator = _stack_coordinator(ledger_service)
    stack_id = StackId("stack-1")
    _merge_ready_stack(coordinator, stack_id=stack_id)
    pull_requests = FakePullRequests(_evidence(), _merged_snapshot())
    service = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests, policy=_allow_evaluator()
    )

    outcome = service.merge_next(
        stack_id,
        pull_request_number=11,
        required_checks=("ci",),
        require_approved_review=False,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )

    assert outcome.merged is True
    assert outcome.milestone_id == PlanMilestoneId("m1")
    assert outcome.stack.slice(PlanMilestoneId("m1")).state is StackSliceState.MERGED
    assert coordinator.read(stack_id) == outcome.stack
    assert pull_requests.merge_calls == [(11, _HEAD)]


def test_returns_not_merged_when_no_slice_is_eligible(ledger_service: LedgerService) -> None:
    coordinator = _stack_coordinator(ledger_service)
    stack_id = StackId("stack-1")
    coordinator.start(
        stack_id=stack_id,
        plan_id=PlanId("plan-1"),
        base_ref="main",
        ordered_milestones=_MILESTONES,
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    pull_requests = FakePullRequests(_evidence(), _merged_snapshot())
    service = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests, policy=_allow_evaluator()
    )

    outcome = service.merge_next(
        stack_id,
        pull_request_number=11,
        required_checks=("ci",),
        require_approved_review=False,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )

    assert outcome.merged is False
    assert outcome.milestone_id is None
    assert pull_requests.merge_calls == []


def test_raises_when_stack_does_not_exist(ledger_service: LedgerService) -> None:
    coordinator = _stack_coordinator(ledger_service)
    pull_requests = FakePullRequests(_evidence(), _merged_snapshot())
    service = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests, policy=_allow_evaluator()
    )

    with pytest.raises(MissingPrerequisiteError):
        service.merge_next(
            StackId("missing"),
            pull_request_number=11,
            required_checks=("ci",),
            require_approved_review=False,
            risk_class=RiskClass.LOW,
            requesting_principal_id="operator-1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )


def test_denies_merge_when_policy_does_not_allow_the_risk_class(
    ledger_service: LedgerService,
) -> None:
    coordinator = _stack_coordinator(ledger_service)
    stack_id = StackId("stack-1")
    _merge_ready_stack(coordinator, stack_id=stack_id)
    pull_requests = FakePullRequests(_evidence(), _merged_snapshot())
    service = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests, policy=_allow_evaluator()
    )

    with pytest.raises(PolicyDenialError):
        service.merge_next(
            stack_id,
            pull_request_number=11,
            required_checks=("ci",),
            require_approved_review=False,
            risk_class=RiskClass.HIGH,
            requesting_principal_id="operator-1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
    assert pull_requests.merge_calls == []


def test_does_not_merge_when_fresh_evidence_shows_a_stale_head(
    ledger_service: LedgerService,
) -> None:
    coordinator = _stack_coordinator(ledger_service)
    stack_id = StackId("stack-1")
    _merge_ready_stack(coordinator, stack_id=stack_id)
    pull_requests = FakePullRequests(_evidence(head="m1-rev2-force-pushed"), _merged_snapshot())
    service = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests, policy=_allow_evaluator()
    )

    outcome = service.merge_next(
        stack_id,
        pull_request_number=11,
        required_checks=("ci",),
        require_approved_review=False,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )

    assert outcome.merged is False
    assert "superseded" in outcome.detail
    assert pull_requests.merge_calls == []
    # the durable record is untouched -- still merge_ready, not merged
    reread = coordinator.read(stack_id)
    assert reread is not None
    assert reread.slice(PlanMilestoneId("m1")).state is StackSliceState.MERGE_READY


def test_enforces_root_to_leaf_order_across_repeated_calls(
    ledger_service: LedgerService,
) -> None:
    coordinator = _stack_coordinator(ledger_service)
    stack_id = StackId("stack-1")
    _merge_ready_stack(coordinator, stack_id=stack_id)
    coordinator.reconcile_after_publish(
        stack_id,
        PlanMilestoneId("m2"),
        head_revision="m2-rev1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    coordinator.mark_merge_ready(
        stack_id,
        PlanMilestoneId("m2"),
        head_revision="m2-rev1",
        ci_evidence_digest=Digest.of_bytes(b"ci-passed-m2"),
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    pull_requests = FakePullRequests(_evidence(), _merged_snapshot())
    service = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests, policy=_allow_evaluator()
    )

    first = service.merge_next(
        stack_id,
        pull_request_number=11,
        required_checks=("ci",),
        require_approved_review=False,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    assert first.milestone_id == PlanMilestoneId("m1")

    pull_requests_m2 = FakePullRequests(
        _evidence(head="m2-rev1"),
        PullRequestSnapshot(
            number=12,
            url="https://github.com/Mathews-Tom/enginery-provider-smoke/pull/12",
            state="closed",
            head_branch="feature/m2",
            head_revision="m2-rev1",
            base_branch="feature/m1",
            base_revision="b" * 40,
            merged=True,
        ),
    )
    service_m2 = MergePolicyService(
        stacks=coordinator, pull_requests=pull_requests_m2, policy=_allow_evaluator()
    )
    second = service_m2.merge_next(
        stack_id,
        pull_request_number=12,
        required_checks=("ci",),
        require_approved_review=False,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    assert second.milestone_id == PlanMilestoneId("m2")
    assert second.stack.next_mergeable() is None
