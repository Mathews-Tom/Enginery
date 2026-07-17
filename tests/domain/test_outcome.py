"""Tests for enginery.domain.outcome."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.ids import OutcomeId, RunId, WorkItemId
from enginery.domain.outcome import Outcome, OutcomeKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_outcome(**overrides: object) -> Outcome:
    defaults: dict[str, object] = {
        "id": OutcomeId("outcome-1"),
        "work_item_id": WorkItemId("wi-1"),
        "kind": OutcomeKind.PR_ACCEPTED,
        "observed_at": _NOW,
    }
    defaults.update(overrides)
    return Outcome(**defaults)  # type: ignore[arg-type]


class TestOutcomeKind:
    def test_has_the_eleven_designed_kinds(self) -> None:
        assert {member.value for member in OutcomeKind} == {
            "pr_accepted",
            "pr_rejected",
            "pr_abandoned",
            "merge_result",
            "ci_stability",
            "release_result",
            "deployment_result",
            "rollback",
            "reopened_issue",
            "escaped_defect",
            "user_rated_quality",
        }


class TestOutcome:
    def test_constructs_with_minimal_fields(self) -> None:
        outcome = _make_outcome()

        assert outcome.run_id is None
        assert outcome.linked_work_item_id is None
        assert outcome.schema_version == 1

    def test_accepts_an_optional_run_id(self) -> None:
        outcome = _make_outcome(run_id=RunId("run-1"))

        assert outcome.run_id == RunId("run-1")

    def test_is_immutable(self) -> None:
        outcome = _make_outcome()
        with pytest.raises(AttributeError):
            outcome.kind = OutcomeKind.PR_REJECTED  # type: ignore[misc]

    def test_rejects_naive_observed_at(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_outcome(observed_at=datetime(2026, 1, 1))

    @pytest.mark.parametrize("kind", [OutcomeKind.REOPENED_ISSUE, OutcomeKind.ESCAPED_DEFECT])
    def test_reopened_and_escaped_kinds_require_linked_work_item(self, kind: OutcomeKind) -> None:
        with pytest.raises(Exception, match="linked_work_item_id"):
            _make_outcome(kind=kind)

    @pytest.mark.parametrize("kind", [OutcomeKind.REOPENED_ISSUE, OutcomeKind.ESCAPED_DEFECT])
    def test_reopened_and_escaped_kinds_accept_a_linked_work_item(self, kind: OutcomeKind) -> None:
        outcome = _make_outcome(kind=kind, linked_work_item_id=WorkItemId("wi-2"))

        assert outcome.linked_work_item_id == WorkItemId("wi-2")

    def test_other_kinds_reject_a_linked_work_item(self) -> None:
        with pytest.raises(Exception, match="linked_work_item_id"):
            _make_outcome(kind=OutcomeKind.PR_ACCEPTED, linked_work_item_id=WorkItemId("wi-2"))

    def test_linked_work_item_cannot_equal_the_subject_work_item(self) -> None:
        with pytest.raises(Exception, match="differ"):
            _make_outcome(
                kind=OutcomeKind.REOPENED_ISSUE,
                work_item_id=WorkItemId("wi-1"),
                linked_work_item_id=WorkItemId("wi-1"),
            )

    def test_rejects_schema_version_below_one(self) -> None:
        with pytest.raises(Exception, match="schema_version"):
            _make_outcome(schema_version=0)

    def test_detail_is_defensively_copied_from_the_caller(self) -> None:
        source = {"pr_number": 42}
        outcome = _make_outcome(detail=source)
        source["pr_number"] = 43

        assert outcome.detail["pr_number"] == 42

    def test_detail_cannot_be_mutated_through_the_instance(self) -> None:
        outcome = _make_outcome(detail={"pr_number": 42})

        with pytest.raises(TypeError):
            outcome.detail["pr_number"] = 43  # type: ignore[index]
