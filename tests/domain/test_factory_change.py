"""Tests for enginery.domain.factory_change."""

from __future__ import annotations

import pytest

from enginery.domain.factory_change import (
    FACTORY_CHANGE_TRANSITIONS,
    FactoryChange,
    FactoryChangeState,
)
from enginery.domain.ids import FactoryChangeId
from tests.domain.test_state_machine import TestEveryDomainTransitionTableHasNoDeadEnds


def _make_factory_change(**overrides: object) -> FactoryChange:
    defaults: dict[str, object] = {
        "id": FactoryChangeId("fc-1"),
        "affected_asset": "workflows/issue_to_pr.yaml",
        "baseline_version": "v3",
        "problem_statement": "repair budget exhausted on 12% of runs last month",
        "hypothesis": "raising the repair budget from 2 to 3 reduces exhaustion below 5%",
        "candidate_version": "v4-candidate",
        "state": FactoryChangeState.PROPOSED,
    }
    defaults.update(overrides)
    return FactoryChange(**defaults)  # type: ignore[arg-type]


class TestFactoryChangeState:
    def test_has_the_nine_designed_states(self) -> None:
        assert {member.value for member in FactoryChangeState} == {
            "proposed",
            "evaluation_ready",
            "evaluating",
            "review_required",
            "canary_ready",
            "canarying",
            "promoted",
            "retained",
            "rolled_back",
            "rejected",
        }


class TestFactoryChangeTransitions:
    def test_has_no_dead_ends(self) -> None:
        TestEveryDomainTransitionTableHasNoDeadEnds.assert_every_non_terminal_state_reaches_a_terminal(
            FACTORY_CHANGE_TRANSITIONS
        )

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (FactoryChangeState.PROPOSED, FactoryChangeState.EVALUATION_READY),
            (FactoryChangeState.PROPOSED, FactoryChangeState.REJECTED),
            (FactoryChangeState.EVALUATION_READY, FactoryChangeState.EVALUATING),
            (FactoryChangeState.EVALUATING, FactoryChangeState.REVIEW_REQUIRED),
            (FactoryChangeState.REVIEW_REQUIRED, FactoryChangeState.CANARY_READY),
            (FactoryChangeState.REVIEW_REQUIRED, FactoryChangeState.REJECTED),
            (FactoryChangeState.CANARY_READY, FactoryChangeState.CANARYING),
            (FactoryChangeState.CANARYING, FactoryChangeState.PROMOTED),
            (FactoryChangeState.CANARYING, FactoryChangeState.RETAINED),
            (FactoryChangeState.CANARYING, FactoryChangeState.ROLLED_BACK),
            (FactoryChangeState.RETAINED, FactoryChangeState.EVALUATION_READY),
            (FactoryChangeState.RETAINED, FactoryChangeState.CANARY_READY),
            (FactoryChangeState.RETAINED, FactoryChangeState.REJECTED),
        ],
    )
    def test_every_designed_edge_is_legal(
        self, source: FactoryChangeState, target: FactoryChangeState
    ) -> None:
        assert FACTORY_CHANGE_TRANSITIONS.allows(source, target)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (FactoryChangeState.PROPOSED, FactoryChangeState.EVALUATING),
            (FactoryChangeState.EVALUATING, FactoryChangeState.REJECTED),
            (FactoryChangeState.CANARY_READY, FactoryChangeState.PROMOTED),
            (FactoryChangeState.PROMOTED, FactoryChangeState.RETAINED),
            (FactoryChangeState.REJECTED, FactoryChangeState.PROPOSED),
            (FactoryChangeState.ROLLED_BACK, FactoryChangeState.RETAINED),
        ],
    )
    def test_undesigned_edges_are_illegal(
        self, source: FactoryChangeState, target: FactoryChangeState
    ) -> None:
        assert not FACTORY_CHANGE_TRANSITIONS.allows(source, target)

    def test_terminal_states_are_exactly_promoted_rejected_rolled_back(self) -> None:
        assert FACTORY_CHANGE_TRANSITIONS.terminal_states == frozenset(
            {
                FactoryChangeState.PROMOTED,
                FactoryChangeState.REJECTED,
                FactoryChangeState.ROLLED_BACK,
            }
        )

    def test_retained_is_not_terminal(self) -> None:
        assert not FACTORY_CHANGE_TRANSITIONS.is_terminal(FactoryChangeState.RETAINED)


class TestFactoryChange:
    def test_constructs_with_valid_fields(self) -> None:
        change = _make_factory_change()

        assert change.state is FactoryChangeState.PROPOSED
        assert change.aggregate_version == 0

    def test_is_immutable(self) -> None:
        change = _make_factory_change()
        with pytest.raises(AttributeError):
            change.state = FactoryChangeState.REJECTED  # type: ignore[misc]

    def test_rejects_candidate_version_equal_to_baseline(self) -> None:
        with pytest.raises(Exception, match="differ from baseline_version"):
            _make_factory_change(baseline_version="v3", candidate_version="v3")

    @pytest.mark.parametrize(
        "field_name",
        [
            "affected_asset",
            "baseline_version",
            "problem_statement",
            "hypothesis",
            "candidate_version",
        ],
    )
    def test_rejects_blank_required_fields(self, field_name: str) -> None:
        with pytest.raises(Exception, match="blank"):
            _make_factory_change(**{field_name: "  "})

    def test_rejects_negative_aggregate_version(self) -> None:
        with pytest.raises(Exception, match="aggregate_version"):
            _make_factory_change(aggregate_version=-1)

    def test_transition_to_advances_state_and_increments_version(self) -> None:
        change = _make_factory_change()

        advanced = change.transition_to(FactoryChangeState.EVALUATION_READY)

        assert advanced.state is FactoryChangeState.EVALUATION_READY
        assert advanced.aggregate_version == 1
        assert change.state is FactoryChangeState.PROPOSED

    def test_transition_to_rejects_an_illegal_transition(self) -> None:
        change = _make_factory_change()

        with pytest.raises(Exception, match="illegal transition"):
            change.transition_to(FactoryChangeState.PROMOTED)

    def test_transition_to_rejects_leaving_a_terminal_state(self) -> None:
        change = _make_factory_change(state=FactoryChangeState.PROMOTED)

        with pytest.raises(Exception, match="illegal transition"):
            change.transition_to(FactoryChangeState.RETAINED)

    def test_full_promotion_path_is_traversable(self) -> None:
        change = _make_factory_change()

        promoted = (
            change.transition_to(FactoryChangeState.EVALUATION_READY)
            .transition_to(FactoryChangeState.EVALUATING)
            .transition_to(FactoryChangeState.REVIEW_REQUIRED)
            .transition_to(FactoryChangeState.CANARY_READY)
            .transition_to(FactoryChangeState.CANARYING)
            .transition_to(FactoryChangeState.PROMOTED)
        )

        assert promoted.state is FactoryChangeState.PROMOTED
        assert promoted.aggregate_version == 6

    def test_retained_can_re_enter_evaluation_or_canary_readiness(self) -> None:
        retained = _make_factory_change(state=FactoryChangeState.RETAINED)

        assert retained.transition_to(FactoryChangeState.EVALUATION_READY).state is (
            FactoryChangeState.EVALUATION_READY
        )
        assert retained.transition_to(FactoryChangeState.CANARY_READY).state is (
            FactoryChangeState.CANARY_READY
        )

    def test_comparison_result_is_defensively_copied_from_the_caller(self) -> None:
        source = {"exhaustion_rate_delta": -0.08}
        change = _make_factory_change(comparison_result=source)
        source["exhaustion_rate_delta"] = 0.5

        assert change.comparison_result is not None
        assert change.comparison_result["exhaustion_rate_delta"] == -0.08

    def test_comparison_result_cannot_be_mutated_through_the_instance(self) -> None:
        change = _make_factory_change(comparison_result={"exhaustion_rate_delta": -0.08})

        assert change.comparison_result is not None
        with pytest.raises(TypeError):
            change.comparison_result["exhaustion_rate_delta"] = 0.5  # type: ignore[index]
