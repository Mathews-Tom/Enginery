"""Tests for enginery.evaluation.outcomes.OutcomeCaptureService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.application.adapter_types import AdapterStatus
from enginery.application.work_ports import (
    PullRequestEvidence,
    PullRequestRequest,
    PullRequestSnapshot,
)
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ObservationId, OperationId, OutcomeId, RunId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.observation import ObservationRequest, ObservationState
from enginery.domain.outcome import OutcomeKind
from enginery.evaluation.outcomes import (
    CompletenessReport,
    OutcomeCaptureService,
    classify_pull_request_outcome,
    compute_completeness,
    observation_id_for,
)
from enginery.ledger.service import LedgerService

_OPENED = datetime(2026, 1, 1, tzinfo=UTC)
_WORK_ITEM_ID = WorkItemId("wi-1")
_RUN_ID = RunId("run-1")


class _FakePullRequests:
    """A minimal structural PullRequestPort double: only ``get`` is used."""

    def __init__(self) -> None:
        self._snapshots: dict[int, PullRequestSnapshot] = {}

    def set_snapshot(self, number: int, snapshot: PullRequestSnapshot) -> None:
        self._snapshots[number] = snapshot

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused
        raise NotImplementedError

    def create_or_update(
        self, request: PullRequestRequest
    ) -> PullRequestSnapshot:  # pragma: no cover - unused
        raise NotImplementedError

    def get(self, number: int) -> PullRequestSnapshot:
        return self._snapshots[number]

    def evidence(self, number: int) -> PullRequestEvidence:  # pragma: no cover - unused
        raise NotImplementedError

    def merge(
        self,
        number: int,
        *,
        expected_head_revision: str,
        operation_id: OperationId,
        merge_method: str = "merge",
    ) -> PullRequestSnapshot:  # pragma: no cover - unused
        raise NotImplementedError

    def reconcile(
        self, *, operation_id: OperationId
    ) -> ReconciliationResult:  # pragma: no cover - unused
        raise NotImplementedError


def _snapshot(
    *, number: int = 42, state: str = "open", merged: bool = False
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=number,
        url=f"https://example.invalid/pr/{number}",
        state=state,
        head_branch="feature",
        head_revision="a" * 40,
        base_branch="main",
        base_revision="b" * 40,
        merged=merged,
    )


class TestClassifyPullRequestOutcome:
    def test_merged_pr_is_pr_accepted(self) -> None:
        assert classify_pull_request_outcome(_snapshot(merged=True)) is OutcomeKind.PR_ACCEPTED

    def test_closed_unmerged_pr_is_pr_abandoned(self) -> None:
        assert classify_pull_request_outcome(_snapshot(state="closed")) is OutcomeKind.PR_ABANDONED

    def test_open_pr_is_not_yet_classified(self) -> None:
        assert classify_pull_request_outcome(_snapshot(state="open")) is None


class TestObservationIdFor:
    def test_is_deterministic(self) -> None:
        first = observation_id_for(_RUN_ID, OutcomeKind.MERGE_RESULT)
        second = observation_id_for(_RUN_ID, OutcomeKind.MERGE_RESULT)

        assert first == second

    def test_differs_by_kind(self) -> None:
        merge_id = observation_id_for(_RUN_ID, OutcomeKind.MERGE_RESULT)
        reopen_id = observation_id_for(_RUN_ID, OutcomeKind.REOPENED_ISSUE)

        assert merge_id != reopen_id


class TestRegisterPending:
    def test_registers_a_new_pending_observation(self, ledger_service: LedgerService) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)

        observation = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        assert observation.state is ObservationState.PENDING
        assert observation.detail["subject_reference"] == "42"
        assert service.read_observation(observation.id) == observation

    def test_uses_the_default_window_for_the_kind(self, ledger_service: LedgerService) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)

        observation = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        assert observation.window == timedelta(days=14)

    def test_re_registering_the_same_run_and_kind_is_a_no_op(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        first = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        second = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="99",
            opened_at=_OPENED + timedelta(days=1),
        )

        assert second == first
        assert len(service.list_observations()) == 1


class TestCapture:
    def test_captures_a_pending_observation(self, ledger_service: LedgerService) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        observation = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-2",
            opened_at=_OPENED,
        )

        outcome = service.capture(
            observation.id,
            outcome_id=OutcomeId("outcome-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=3),
            detail={"defect": "regression"},
            linked_work_item_id=WorkItemId("wi-2"),
        )

        assert outcome.kind is OutcomeKind.ESCAPED_DEFECT
        assert outcome.work_item_id == _WORK_ITEM_ID
        assert outcome.linked_work_item_id == WorkItemId("wi-2")
        resolved = service.read_observation(observation.id)
        assert resolved is not None
        assert resolved.state is ObservationState.CAPTURED
        assert resolved.outcome_id == outcome.id
        assert service.read_outcome(outcome.id) == outcome

    def test_capturing_an_unregistered_observation_raises(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)

        with pytest.raises(InvalidInputError, match="no observation is registered"):
            service.capture(
                ObservationId("missing"),
                outcome_id=OutcomeId("outcome-1"),
                kind=OutcomeKind.ESCAPED_DEFECT,
                observed_at=_OPENED,
            )

    def test_capturing_an_already_captured_observation_raises(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        observation = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-2",
            opened_at=_OPENED,
        )

        service.capture(
            observation.id,
            outcome_id=OutcomeId("outcome-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-2"),
        )

        with pytest.raises(Exception, match="only a pending observation"):
            service.capture(
                observation.id,
                outcome_id=OutcomeId("outcome-2"),
                kind=OutcomeKind.ESCAPED_DEFECT,
                observed_at=_OPENED + timedelta(days=2),
                linked_work_item_id=WorkItemId("wi-2"),
            )


class TestSweep:
    def test_sweeps_a_merged_pull_request_into_pr_accepted(
        self, ledger_service: LedgerService
    ) -> None:
        pull_requests = _FakePullRequests()
        pull_requests.set_snapshot(42, _snapshot(number=42, merged=True))
        service = OutcomeCaptureService(ledger=ledger_service, pull_requests=pull_requests)
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        captured = service.sweep(reference_time=_OPENED + timedelta(days=1))

        assert len(captured) == 1
        assert captured[0].kind is OutcomeKind.PR_ACCEPTED
        observations = service.list_observations(state=ObservationState.CAPTURED)
        assert len(observations) == 1

    def test_sweep_leaves_an_open_pull_request_pending(self, ledger_service: LedgerService) -> None:
        pull_requests = _FakePullRequests()
        pull_requests.set_snapshot(42, _snapshot(number=42, state="open"))
        service = OutcomeCaptureService(ledger=ledger_service, pull_requests=pull_requests)
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        captured = service.sweep(reference_time=_OPENED + timedelta(days=1))

        assert captured == ()
        assert service.list_observations(state=ObservationState.PENDING) != ()

    def test_sweep_never_auto_captures_reopened_issue_or_escaped_defect(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.REOPENED_ISSUE,
            subject_reference="wi-1",
            opened_at=_OPENED,
        )
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=RunId("run-2"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-1",
            opened_at=_OPENED,
        )

        captured = service.sweep(reference_time=_OPENED + timedelta(days=365))

        assert captured == ()
        assert len(service.list_observations(state=ObservationState.PENDING)) == 2

    def test_sweep_without_a_pull_request_port_leaves_merge_result_pending(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        captured = service.sweep(reference_time=_OPENED + timedelta(days=1))

        assert captured == ()


class TestListing:
    def test_list_observations_filters_by_state(self, ledger_service: LedgerService) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        pending = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )
        captured_source = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=RunId("run-2"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-1",
            opened_at=_OPENED,
        )

        service.capture(
            captured_source.id,
            outcome_id=OutcomeId("outcome-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-2"),
        )

        all_observations = service.list_observations()
        only_pending = service.list_observations(state=ObservationState.PENDING)

        assert len(all_observations) == 2
        assert only_pending == (pending,)


class TestExpireOverdue:
    def test_expires_a_pending_observation_past_its_window(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        observation = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        expired = service.expire_overdue(reference_time=_OPENED + observation.window)

        assert len(expired) == 1
        assert expired[0].state is ObservationState.INDETERMINATE
        resolved = service.read_observation(observation.id)
        assert resolved is not None
        assert resolved.state is ObservationState.INDETERMINATE

    def test_leaves_an_observation_inside_its_window_pending(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="42",
            opened_at=_OPENED,
        )

        expired = service.expire_overdue(reference_time=_OPENED + timedelta(days=1))

        assert expired == ()
        assert len(service.list_observations(state=ObservationState.PENDING)) == 1

    def test_never_expires_an_already_captured_observation(
        self, ledger_service: LedgerService
    ) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        observation = service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-2",
            opened_at=_OPENED,
        )
        service.capture(
            observation.id,
            outcome_id=OutcomeId("outcome-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-2"),
        )

        expired = service.expire_overdue(reference_time=_OPENED + observation.window)

        assert expired == ()
        resolved = service.read_observation(observation.id)
        assert resolved is not None
        assert resolved.state is ObservationState.CAPTURED


class TestComputeCompleteness:
    def _observation(self, **overrides: object) -> ObservationRequest:
        defaults: dict[str, object] = {
            "id": ObservationId("obs-1"),
            "work_item_id": _WORK_ITEM_ID,
            "run_id": _RUN_ID,
            "kind": OutcomeKind.MERGE_RESULT,
            "opened_at": _OPENED,
            "window": timedelta(days=14),
        }
        defaults.update(overrides)
        return ObservationRequest(**defaults)  # type: ignore[arg-type]

    def test_reports_the_current_derivation_version(self) -> None:
        report = compute_completeness((), reference_time=_OPENED)

        assert report.derivation_version == 1

    def test_full_completeness_with_no_indeterminate_observations(self) -> None:
        captured = self._observation(
            state=ObservationState.CAPTURED,
            resolved_at=_OPENED + timedelta(days=1),
            outcome_id=OutcomeId("outcome-1"),
        )

        report = compute_completeness((captured,), reference_time=_OPENED)

        assert report.completeness == 1.0
        assert report.captured == 1
        assert report.indeterminate == 0

    def test_completeness_with_no_decided_observations_defaults_to_one(self) -> None:
        pending = self._observation()

        report = compute_completeness((pending,), reference_time=_OPENED)

        assert report.completeness == 1.0
        assert report.pending == 1

    def test_an_indeterminate_observation_lowers_completeness(self) -> None:
        captured = self._observation(
            id=ObservationId("obs-1"),
            state=ObservationState.CAPTURED,
            resolved_at=_OPENED + timedelta(days=1),
            outcome_id=OutcomeId("outcome-1"),
        )
        indeterminate = self._observation(
            id=ObservationId("obs-2"),
            state=ObservationState.INDETERMINATE,
            resolved_at=_OPENED + timedelta(days=14),
        )

        report = compute_completeness((captured, indeterminate), reference_time=_OPENED)

        assert report.completeness == 0.5
        assert report.captured == 1
        assert report.indeterminate == 1

    def test_a_pending_observation_never_inflates_completeness(self) -> None:
        """Suppressing capture (leaving an observation pending forever)
        must never raise completeness -- only sweeping it to
        ``indeterminate`` after its window elapses can move the ratio,
        and only downward."""
        captured = self._observation(
            id=ObservationId("obs-1"),
            state=ObservationState.CAPTURED,
            resolved_at=_OPENED + timedelta(days=1),
            outcome_id=OutcomeId("outcome-1"),
        )
        suppressed_pending = self._observation(id=ObservationId("obs-2"))

        with_pending = compute_completeness((captured, suppressed_pending), reference_time=_OPENED)
        without_pending = compute_completeness((captured,), reference_time=_OPENED)

        assert with_pending.completeness == without_pending.completeness == 1.0

    def test_expiring_a_suppressed_observation_lowers_completeness(self) -> None:
        """The end-to-end proof: a capture that never happens still shows
        up as a completeness gap once its window elapses and is swept."""
        captured = self._observation(
            id=ObservationId("obs-1"),
            state=ObservationState.CAPTURED,
            resolved_at=_OPENED + timedelta(days=1),
            outcome_id=OutcomeId("outcome-1"),
        )
        expired_from_suppression = self._observation(
            id=ObservationId("obs-2"),
            state=ObservationState.INDETERMINATE,
            resolved_at=_OPENED + timedelta(days=14),
        )

        report = compute_completeness(
            (captured, expired_from_suppression), reference_time=_OPENED + timedelta(days=15)
        )

        assert report.completeness < 1.0


class TestServiceCompleteness:
    def test_completeness_reads_through_the_ledger(self, ledger_service: LedgerService) -> None:
        service = OutcomeCaptureService(ledger=ledger_service)
        service.register_pending(
            work_item_id=_WORK_ITEM_ID,
            run_id=_RUN_ID,
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-2",
            opened_at=_OPENED,
        )

        report = service.completeness(reference_time=_OPENED)

        assert isinstance(report, CompletenessReport)
        assert report.pending == 1
        assert report.completeness == 1.0
