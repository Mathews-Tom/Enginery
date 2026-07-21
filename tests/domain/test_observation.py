"""Tests for enginery.domain.observation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.domain.ids import ObservationId, OutcomeId, RunId, WorkItemId
from enginery.domain.observation import ObservationRequest, ObservationState
from enginery.domain.outcome import OutcomeKind

_OPENED = datetime(2026, 1, 1, tzinfo=UTC)
_WINDOW = timedelta(days=7)


def _make_observation(**overrides: object) -> ObservationRequest:
    defaults: dict[str, object] = {
        "id": ObservationId("obs-1"),
        "work_item_id": WorkItemId("wi-1"),
        "run_id": RunId("run-1"),
        "kind": OutcomeKind.MERGE_RESULT,
        "opened_at": _OPENED,
        "window": _WINDOW,
    }
    defaults.update(overrides)
    return ObservationRequest(**defaults)  # type: ignore[arg-type]


class TestObservationRequestConstruction:
    def test_constructs_pending_with_minimal_fields(self) -> None:
        observation = _make_observation()

        assert observation.state is ObservationState.PENDING
        assert observation.resolved_at is None
        assert observation.outcome_id is None
        assert observation.schema_version == 1

    def test_is_immutable(self) -> None:
        observation = _make_observation()
        with pytest.raises(AttributeError):
            observation.state = ObservationState.CAPTURED  # type: ignore[misc]

    def test_rejects_naive_opened_at(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_observation(opened_at=datetime(2026, 1, 1))

    def test_rejects_non_positive_window(self) -> None:
        with pytest.raises(Exception, match="window"):
            _make_observation(window=timedelta(0))

    def test_due_at_is_opened_at_plus_window(self) -> None:
        observation = _make_observation()

        assert observation.due_at == _OPENED + _WINDOW

    def test_pending_rejects_resolved_at(self) -> None:
        with pytest.raises(Exception, match="resolved_at"):
            _make_observation(resolved_at=_OPENED)

    def test_pending_rejects_outcome_id(self) -> None:
        with pytest.raises(Exception, match="outcome_id"):
            _make_observation(outcome_id=OutcomeId("outcome-1"))

    def test_captured_requires_resolved_at(self) -> None:
        with pytest.raises(Exception, match="resolved_at"):
            _make_observation(state=ObservationState.CAPTURED, outcome_id=OutcomeId("outcome-1"))

    def test_captured_requires_outcome_id(self) -> None:
        with pytest.raises(Exception, match="outcome_id"):
            _make_observation(
                state=ObservationState.CAPTURED, resolved_at=_OPENED + timedelta(days=1)
            )

    def test_indeterminate_rejects_outcome_id(self) -> None:
        with pytest.raises(Exception, match="outcome_id"):
            _make_observation(
                state=ObservationState.INDETERMINATE,
                resolved_at=_OPENED + _WINDOW,
                outcome_id=OutcomeId("outcome-1"),
            )

    def test_resolved_at_cannot_precede_opened_at(self) -> None:
        with pytest.raises(Exception, match="precede"):
            _make_observation(
                state=ObservationState.INDETERMINATE,
                resolved_at=_OPENED - timedelta(days=1),
            )

    def test_rejects_schema_version_below_one(self) -> None:
        with pytest.raises(Exception, match="schema_version"):
            _make_observation(schema_version=0)

    def test_detail_is_defensively_copied_from_the_caller(self) -> None:
        source = {"pr_number": 42}
        observation = _make_observation(detail=source)
        source["pr_number"] = 43

        assert observation.detail["pr_number"] == 42

    def test_detail_cannot_be_mutated_through_the_instance(self) -> None:
        observation = _make_observation(detail={"pr_number": 42})

        with pytest.raises(TypeError):
            observation.detail["pr_number"] = 43  # type: ignore[index]


class TestIsOverdue:
    def test_pending_before_window_elapses_is_not_overdue(self) -> None:
        observation = _make_observation()

        assert not observation.is_overdue(reference_time=_OPENED + timedelta(days=1))

    def test_pending_at_or_after_window_elapses_is_overdue(self) -> None:
        observation = _make_observation()

        assert observation.is_overdue(reference_time=_OPENED + _WINDOW)
        assert observation.is_overdue(reference_time=_OPENED + _WINDOW + timedelta(days=1))

    def test_captured_is_never_overdue(self) -> None:
        observation = _make_observation().resolve_captured(
            outcome_id=OutcomeId("outcome-1"), resolved_at=_OPENED + timedelta(days=1)
        )

        assert not observation.is_overdue(reference_time=_OPENED + _WINDOW + timedelta(days=365))


class TestResolveCaptured:
    def test_resolves_a_pending_observation(self) -> None:
        observation = _make_observation()
        resolved_at = _OPENED + timedelta(days=1)

        resolved = observation.resolve_captured(
            outcome_id=OutcomeId("outcome-1"), resolved_at=resolved_at
        )

        assert resolved.state is ObservationState.CAPTURED
        assert resolved.outcome_id == OutcomeId("outcome-1")
        assert resolved.resolved_at == resolved_at
        # the original instance is untouched
        assert observation.state is ObservationState.PENDING

    def test_cannot_resolve_an_already_captured_observation(self) -> None:
        observation = _make_observation().resolve_captured(
            outcome_id=OutcomeId("outcome-1"), resolved_at=_OPENED + timedelta(days=1)
        )

        with pytest.raises(Exception, match="only a pending observation"):
            observation.resolve_captured(
                outcome_id=OutcomeId("outcome-2"), resolved_at=_OPENED + timedelta(days=2)
            )


class TestResolveIndeterminate:
    def test_resolves_a_pending_observation_past_its_window(self) -> None:
        observation = _make_observation()
        resolved_at = _OPENED + _WINDOW

        resolved = observation.resolve_indeterminate(resolved_at=resolved_at)

        assert resolved.state is ObservationState.INDETERMINATE
        assert resolved.outcome_id is None
        assert resolved.resolved_at == resolved_at

    def test_cannot_resolve_indeterminate_before_the_window_elapses(self) -> None:
        observation = _make_observation()

        with pytest.raises(Exception, match="cannot become indeterminate"):
            observation.resolve_indeterminate(resolved_at=_OPENED + timedelta(days=1))

    def test_cannot_resolve_an_already_indeterminate_observation(self) -> None:
        observation = _make_observation().resolve_indeterminate(resolved_at=_OPENED + _WINDOW)

        with pytest.raises(Exception, match="only a pending observation"):
            observation.resolve_indeterminate(resolved_at=_OPENED + _WINDOW + timedelta(days=1))
