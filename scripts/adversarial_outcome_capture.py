"""Reject suppression and delayed-attribution gaming of outcome capture.

Covers the cases M14a requires: a suppressed (never-captured) observation
must never inflate completeness while it is still inside its window;
letting its window elapse must lower completeness, never raise it; a
capture attempted after expiry must be rejected rather than retroactively
overwriting the indeterminate result with a favorable one; re-registering
the same subject must never create a second, cherry-pickable observation;
and the raw observation/outcome records the derivation reads from must
never mutate when completeness is recomputed at a different reference
time.
"""

from __future__ import annotations

import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ObservationId, OutcomeId, RunId, WorkItemId
from enginery.domain.observation import ObservationState
from enginery.domain.outcome import OutcomeKind
from enginery.evaluation.outcomes import (
    OutcomeCaptureService,
    observation_id_for,
)
from enginery.ledger.service import LedgerService

_OPENED = datetime(2026, 1, 1, tzinfo=UTC)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _expect(exception_type: type[Exception], operation: object) -> None:
    try:
        operation()  # type: ignore[operator]
    except exception_type:
        return
    raise RuntimeError(f"expected {exception_type.__name__} was not raised")


def _scratch_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="enginery-outcome-gate-"))


def _case_suppression_cannot_inflate_completeness_while_pending() -> None:
    """A never-captured observation still inside its window must be
    excluded from both terms of the completeness ratio -- never raise it,
    and never lower it either, until its window actually elapses."""
    service = OutcomeCaptureService(ledger=LedgerService.open(_scratch_root() / "ledger.db"))
    try:
        captured_source = service.register_pending(
            work_item_id=WorkItemId("wi-honest"),
            run_id=RunId("run-honest"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-honest",
            opened_at=_OPENED,
        )
        service.capture(
            captured_source.id,
            outcome_id=OutcomeId("outcome-honest"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-honest-linked"),
        )
        baseline = service.completeness(reference_time=_OPENED + timedelta(days=1))

        service.register_pending(
            work_item_id=WorkItemId("wi-suppressed"),
            run_id=RunId("run-suppressed"),
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="99",
            opened_at=_OPENED,
        )
        with_suppressed_pending = service.completeness(reference_time=_OPENED + timedelta(days=1))

        _assert(
            with_suppressed_pending.completeness == baseline.completeness,
            "a pending observation changed completeness before its window elapsed",
        )
        _assert(
            with_suppressed_pending.pending == 1,
            "the suppressed observation was not reported as pending",
        )
    finally:
        service.ledger.close()


def _case_expiring_a_suppressed_observation_lowers_completeness() -> None:
    """Once a suppressed observation's window elapses and is swept, it
    must show up as a completeness gap -- suppression can only ever cost
    the metric, never help it."""
    service = OutcomeCaptureService(ledger=LedgerService.open(_scratch_root() / "ledger.db"))
    try:
        captured_source = service.register_pending(
            work_item_id=WorkItemId("wi-honest"),
            run_id=RunId("run-honest"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-honest",
            opened_at=_OPENED,
        )
        service.capture(
            captured_source.id,
            outcome_id=OutcomeId("outcome-honest"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-honest-linked"),
        )
        suppressed = service.register_pending(
            work_item_id=WorkItemId("wi-suppressed"),
            run_id=RunId("run-suppressed"),
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="99",
            opened_at=_OPENED,
        )

        before_expiry = service.completeness(reference_time=_OPENED + suppressed.window)
        _assert(
            before_expiry.completeness == 1.0, "an unswept overdue observation must stay excluded"
        )

        expired = service.expire_overdue(reference_time=_OPENED + suppressed.window)
        _assert(
            len(expired) == 1, "expire_overdue did not sweep the overdue suppressed observation"
        )

        after_expiry = service.completeness(reference_time=_OPENED + suppressed.window)
        _assert(
            after_expiry.completeness < before_expiry.completeness,
            "expiring a suppressed observation did not lower completeness",
        )
        _assert(after_expiry.indeterminate == 1, "the expired observation was not counted as a gap")
    finally:
        service.ledger.close()


def _case_a_capture_after_expiry_cannot_retroactively_rewrite_the_gap() -> None:
    """Once an observation has expired to indeterminate, a late "actually
    it merged" signal must never be allowed to overwrite that gap with a
    favorable capture -- delayed attribution cannot buy back completeness."""
    service = OutcomeCaptureService(ledger=LedgerService.open(_scratch_root() / "ledger.db"))
    try:
        observation = service.register_pending(
            work_item_id=WorkItemId("wi-1"),
            run_id=RunId("run-1"),
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="17",
            opened_at=_OPENED,
        )
        service.expire_overdue(reference_time=_OPENED + observation.window)
        resolved = service.read_observation(observation.id)
        _assert(
            resolved is not None and resolved.state is ObservationState.INDETERMINATE,
            "observation did not expire to indeterminate",
        )

        _expect(
            InvalidInputError,
            lambda: service.capture(
                observation.id,
                outcome_id=OutcomeId("outcome-late"),
                kind=OutcomeKind.PR_ACCEPTED,
                observed_at=_OPENED + observation.window + timedelta(days=1),
            ),
        )
        still_indeterminate = service.read_observation(observation.id)
        _assert(
            still_indeterminate is not None
            and still_indeterminate.state is ObservationState.INDETERMINATE
            and still_indeterminate.outcome_id is None,
            "a late capture attempt mutated an already-expired observation",
        )
    finally:
        service.ledger.close()


def _case_re_registration_cannot_create_a_second_cherry_pickable_observation() -> None:
    """Re-registering the same run/kind pair must return the existing
    record, never create a sibling observation an operator could report
    selectively (the favorable one) while ignoring the other."""
    service = OutcomeCaptureService(ledger=LedgerService.open(_scratch_root() / "ledger.db"))
    try:
        run_id = RunId("run-1")
        first = service.register_pending(
            work_item_id=WorkItemId("wi-1"),
            run_id=run_id,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="17",
            opened_at=_OPENED,
        )
        second = service.register_pending(
            work_item_id=WorkItemId("wi-1"),
            run_id=run_id,
            kind=OutcomeKind.MERGE_RESULT,
            subject_reference="different-subject-attempted-here",
            opened_at=_OPENED + timedelta(days=1),
        )
        _assert(first == second, "re-registration produced a different observation record")
        _assert(
            first.id == observation_id_for(run_id, OutcomeKind.MERGE_RESULT),
            "observation identity is not deterministic for its (run_id, kind) pair",
        )
        all_observations = service.list_observations()
        _assert(
            len(all_observations) == 1,
            "re-registration created a second observation for the same subject",
        )
    finally:
        service.ledger.close()


def _case_completeness_recomputation_never_mutates_raw_records() -> None:
    """The versioned derivation is a pure read: calling it repeatedly at
    different reference times must never alter the raw observation or
    outcome records it reads from."""
    service = OutcomeCaptureService(ledger=LedgerService.open(_scratch_root() / "ledger.db"))
    try:
        captured_source = service.register_pending(
            work_item_id=WorkItemId("wi-1"),
            run_id=RunId("run-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            subject_reference="wi-1",
            opened_at=_OPENED,
        )
        outcome = service.capture(
            captured_source.id,
            outcome_id=OutcomeId("outcome-1"),
            kind=OutcomeKind.ESCAPED_DEFECT,
            observed_at=_OPENED + timedelta(days=1),
            linked_work_item_id=WorkItemId("wi-2"),
        )
        before_observation = service.read_observation(captured_source.id)
        before_outcome = service.read_outcome(outcome.id)

        for offset_days in (0, 30, 365, 3650):
            service.completeness(reference_time=_OPENED + timedelta(days=offset_days))

        after_observation = service.read_observation(captured_source.id)
        after_outcome = service.read_outcome(outcome.id)
        _assert(
            before_observation == after_observation,
            "recomputing completeness mutated the raw observation record",
        )
        _assert(
            before_outcome == after_outcome,
            "recomputing completeness mutated the raw outcome record",
        )
    finally:
        service.ledger.close()


def _case_unregistered_observation_cannot_be_captured() -> None:
    """An operator cannot fabricate a favorable outcome for a subject that
    was never registered as an observation -- every captured outcome must
    trace back to a durable, previously registered watch."""
    service = OutcomeCaptureService(ledger=LedgerService.open(_scratch_root() / "ledger.db"))
    try:
        _expect(
            InvalidInputError,
            lambda: service.capture(
                ObservationId("never-registered"),
                outcome_id=OutcomeId("outcome-fabricated"),
                kind=OutcomeKind.PR_ACCEPTED,
                observed_at=_OPENED,
            ),
        )
    finally:
        service.ledger.close()


def run_gate() -> None:
    seed = secrets.randbits(64)
    cases = (
        _case_suppression_cannot_inflate_completeness_while_pending,
        _case_expiring_a_suppressed_observation_lowers_completeness,
        _case_a_capture_after_expiry_cannot_retroactively_rewrite_the_gap,
        _case_re_registration_cannot_create_a_second_cherry_pickable_observation,
        _case_completeness_recomputation_never_mutates_raw_records,
        _case_unregistered_observation_cannot_be_captured,
    )
    for case in cases:
        case()
    print(f"PASS outcome adversarial cases={len(cases)} seed={seed}")


if __name__ == "__main__":
    run_gate()
