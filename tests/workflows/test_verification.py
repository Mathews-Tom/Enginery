from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from enginery.application.work_ports import (
    LifecycleProjection,
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestPort,
    PullRequestReview,
    PullRequestSnapshot,
    WorkLedgerPort,
    WorkLedgerSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.ids import OperationId, RunId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.workflows.pull_request import PullRequestOutcome, PullRequestRequirements
from enginery.workflows.verification import (
    Stage1VerificationExecutor,
    Stage1VerificationRequest,
)


def _snapshot(revision: str = "issue-1") -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("work-1"),
            work_kind=WorkKind.ISSUE,
            source_provider="github",
            external_reference="issue:1",
            source_snapshot_reference=f"issue:1@{revision}",
            title="Bounded change",
            objective="Change one bounded behavior.",
            acceptance_criteria=("observable result",),
            constraints=("retain evidence",),
            risk_class=RiskClass.LOW,
            repository_targets=("repository-1",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision=revision,
    )


def _evidence(base: str = "base", head: str = "head") -> PullRequestEvidence:
    return PullRequestEvidence(
        pull_request=PullRequestSnapshot(
            number=1,
            url="https://example.invalid/pull/1",
            state="open",
            head_branch="feature",
            head_revision=head,
            base_branch="main",
            base_revision=base,
        ),
        reviews=(PullRequestReview("reviewer", "APPROVED", head),),
        checks=(PullRequestCheck("CI", "completed", "success", head),),
        mergeable=True,
    )


class SequenceWorkLedger:
    def __init__(self, snapshots: tuple[WorkLedgerSnapshot, ...]) -> None:
        self._snapshots = iter(snapshots)

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        assert operation_id == OperationId("lifecycle-1")
        assert projection.state == PullRequestOutcome.MERGE_READY.value
        return ReconciliationResult.FOUND_MATCHING

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        assert external_reference == "issue:1"
        return next(self._snapshots)


class SequencePullRequests:
    def __init__(self, evidence: tuple[PullRequestEvidence, ...]) -> None:
        self._evidence = iter(evidence)

    def evidence(self, number: int) -> PullRequestEvidence:
        assert number == 1
        return next(self._evidence)


def _request(snapshot: WorkLedgerSnapshot) -> Stage1VerificationRequest:
    return Stage1VerificationRequest(
        external_reference="issue:1",
        issue_revision=snapshot.source_revision,
        run_id=RunId("run-1"),
        lifecycle_operation_id=OperationId("lifecycle-1"),
        issue_digest=str(snapshot.work_item.bound_field_digest),
        base_revision="base",
        pull_request_number=1,
        requirements=PullRequestRequirements(
            expected_head_revision="head", required_checks=("CI",), require_approved_review=True
        ),
        implementation_artifacts=(Digest.of_bytes(b"implementation"),),
        verification_artifacts=(Digest.of_bytes(b"verification"),),
    )


def test_terminal_verification_emits_evidence_only_after_double_read() -> None:
    snapshot = _snapshot()
    executor = Stage1VerificationExecutor(
        cast(WorkLedgerPort, SequenceWorkLedger((snapshot, snapshot))),
        cast(PullRequestPort, SequencePullRequests((_evidence(), _evidence()))),
    )

    result = executor.verify(
        request=_request(snapshot), observed_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    )

    assert result.outcome is PullRequestOutcome.MERGE_READY
    assert result.evidence is not None
    assert result.evidence.current_for(
        issue_revision="issue-1", base_revision="base", head_revision="head"
    )


def test_terminal_verification_rejects_source_change_between_reads() -> None:
    snapshot = _snapshot()
    changed = replace(snapshot, source_revision="issue-2")
    executor = Stage1VerificationExecutor(
        cast(WorkLedgerPort, SequenceWorkLedger((snapshot, changed))),
        cast(PullRequestPort, SequencePullRequests((_evidence(), _evidence()))),
    )

    result = executor.verify(
        request=_request(snapshot), observed_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    )

    assert result.outcome is PullRequestOutcome.SUPERSEDED
    assert result.evidence is None


def test_terminal_verification_rejects_base_advance_between_reads() -> None:
    snapshot = _snapshot()
    executor = Stage1VerificationExecutor(
        cast(WorkLedgerPort, SequenceWorkLedger((snapshot, snapshot))),
        cast(
            PullRequestPort,
            SequencePullRequests((_evidence(), _evidence(base="advanced-base"))),
        ),
    )

    result = executor.verify(
        request=_request(snapshot), observed_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    )

    assert result.outcome is PullRequestOutcome.SUPERSEDED
    assert result.evidence is None


def test_terminal_verification_rejects_head_change_between_reads() -> None:
    snapshot = _snapshot()
    executor = Stage1VerificationExecutor(
        cast(WorkLedgerPort, SequenceWorkLedger((snapshot, snapshot))),
        cast(
            PullRequestPort,
            SequencePullRequests((_evidence(), _evidence(head="advanced-head"))),
        ),
    )

    result = executor.verify(
        request=_request(snapshot), observed_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    )

    assert result.outcome is PullRequestOutcome.SUPERSEDED
    assert result.evidence is None
