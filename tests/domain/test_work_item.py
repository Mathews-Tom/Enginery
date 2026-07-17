"""Tests for enginery.domain.work_item."""

from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.ids import WorkItemId
from enginery.domain.work_item import WORK_ITEM_TRANSITIONS, WorkItem, WorkItemState
from tests.domain.test_state_machine import TestEveryDomainTransitionTableHasNoDeadEnds


def _make_work_item(**overrides: object) -> WorkItem:
    defaults: dict[str, object] = {
        "id": WorkItemId("wi-1"),
        "work_kind": WorkKind.ISSUE,
        "source_provider": "github",
        "external_reference": "org/repo#42",
        "source_snapshot_reference": "abc123",
        "title": "Fix the thing",
        "objective": "Make the thing work correctly",
        "acceptance_criteria": ("criterion one",),
        "constraints": (),
        "risk_class": RiskClass.LOW,
        "repository_targets": ("org/repo",),
        "dependencies": (),
        "state": WorkItemState.NEW,
        "aggregate_version": 0,
    }
    defaults.update(overrides)
    return WorkItem(**defaults)  # type: ignore[arg-type]


class TestWorkItemState:
    def test_has_the_ten_designed_states(self) -> None:
        assert {member.value for member in WorkItemState} == {
            "new",
            "qualifying",
            "ready",
            "active",
            "blocked",
            "outcome_pending",
            "completed",
            "rejected",
            "cancelled",
            "failed",
        }


class TestWorkItem:
    def test_constructs_with_valid_fields(self) -> None:
        item = _make_work_item()

        assert item.state is WorkItemState.NEW
        assert item.aggregate_version == 0

    def test_is_immutable(self) -> None:
        item = _make_work_item()
        with pytest.raises(AttributeError):
            item.title = "changed"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "field_name",
        [
            "source_provider",
            "external_reference",
            "source_snapshot_reference",
            "title",
            "objective",
        ],
    )
    def test_rejects_blank_required_string_fields(self, field_name: str) -> None:
        with pytest.raises(Exception, match="blank"):
            _make_work_item(**{field_name: "   "})

    def test_rejects_empty_acceptance_criteria(self) -> None:
        with pytest.raises(Exception, match="acceptance criterion"):
            _make_work_item(acceptance_criteria=())

    def test_rejects_empty_repository_targets(self) -> None:
        with pytest.raises(Exception, match="repository target"):
            _make_work_item(repository_targets=())

    def test_rejects_self_dependency(self) -> None:
        work_item_id = WorkItemId("wi-self")
        with pytest.raises(Exception, match="cannot depend on itself"):
            _make_work_item(id=work_item_id, dependencies=(work_item_id,))

    def test_rejects_negative_aggregate_version(self) -> None:
        with pytest.raises(Exception, match="aggregate_version"):
            _make_work_item(aggregate_version=-1)

    def test_bound_field_digest_ignores_unbound_fields(self) -> None:
        base = _make_work_item()
        renamed = replace(base, title="a completely different title")

        assert base.bound_field_digest == renamed.bound_field_digest

    def test_bound_field_digest_changes_with_acceptance_criteria(self) -> None:
        base = _make_work_item()
        changed = replace(base, acceptance_criteria=("a different criterion",))

        assert base.bound_field_digest != changed.bound_field_digest

    def test_bound_field_digest_changes_with_dependencies(self) -> None:
        base = _make_work_item()
        changed = replace(base, dependencies=(WorkItemId("wi-2"),))

        assert base.bound_field_digest != changed.bound_field_digest

    def test_bound_field_digest_changes_with_constraints(self) -> None:
        base = _make_work_item()
        changed = replace(base, constraints=("must not touch prod",))

        assert base.bound_field_digest != changed.bound_field_digest

    def test_bound_field_digest_changes_with_repository_targets(self) -> None:
        base = _make_work_item()
        changed = replace(base, repository_targets=("org/other-repo",))

        assert base.bound_field_digest != changed.bound_field_digest

    def test_bound_field_digest_is_order_sensitive_for_acceptance_criteria(self) -> None:
        base = _make_work_item(acceptance_criteria=("a", "b"))
        reordered = replace(base, acceptance_criteria=("b", "a"))

        assert base.bound_field_digest != reordered.bound_field_digest


class TestWorkItemTransitions:
    def test_has_no_dead_ends(self) -> None:
        TestEveryDomainTransitionTableHasNoDeadEnds.assert_every_non_terminal_state_reaches_a_terminal(
            WORK_ITEM_TRANSITIONS
        )

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (WorkItemState.NEW, WorkItemState.QUALIFYING),
            (WorkItemState.QUALIFYING, WorkItemState.READY),
            (WorkItemState.QUALIFYING, WorkItemState.BLOCKED),
            (WorkItemState.QUALIFYING, WorkItemState.REJECTED),
            (WorkItemState.READY, WorkItemState.ACTIVE),
            (WorkItemState.READY, WorkItemState.CANCELLED),
            (WorkItemState.ACTIVE, WorkItemState.OUTCOME_PENDING),
            (WorkItemState.ACTIVE, WorkItemState.BLOCKED),
            (WorkItemState.ACTIVE, WorkItemState.CANCELLED),
            (WorkItemState.ACTIVE, WorkItemState.FAILED),
            (WorkItemState.BLOCKED, WorkItemState.QUALIFYING),
            (WorkItemState.BLOCKED, WorkItemState.ACTIVE),
            (WorkItemState.BLOCKED, WorkItemState.REJECTED),
            (WorkItemState.BLOCKED, WorkItemState.CANCELLED),
            (WorkItemState.OUTCOME_PENDING, WorkItemState.COMPLETED),
            (WorkItemState.OUTCOME_PENDING, WorkItemState.BLOCKED),
            (WorkItemState.OUTCOME_PENDING, WorkItemState.FAILED),
        ],
    )
    def test_every_designed_edge_is_legal(
        self, source: WorkItemState, target: WorkItemState
    ) -> None:
        assert WORK_ITEM_TRANSITIONS.allows(source, target)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (WorkItemState.NEW, WorkItemState.ACTIVE),
            (WorkItemState.READY, WorkItemState.QUALIFYING),
            (WorkItemState.COMPLETED, WorkItemState.ACTIVE),
            (WorkItemState.REJECTED, WorkItemState.NEW),
        ],
    )
    def test_undesigned_edges_are_illegal(
        self, source: WorkItemState, target: WorkItemState
    ) -> None:
        assert not WORK_ITEM_TRANSITIONS.allows(source, target)

    def test_terminal_states_are_exactly_the_four_designed_terminals(self) -> None:
        assert WORK_ITEM_TRANSITIONS.terminal_states == frozenset(
            {
                WorkItemState.COMPLETED,
                WorkItemState.REJECTED,
                WorkItemState.CANCELLED,
                WorkItemState.FAILED,
            }
        )

    def test_blocked_is_not_terminal_for_a_work_item(self) -> None:
        assert not WORK_ITEM_TRANSITIONS.is_terminal(WorkItemState.BLOCKED)

    def test_transition_to_advances_state_and_increments_version(self) -> None:
        item = _make_work_item()

        advanced = item.transition_to(WorkItemState.QUALIFYING)

        assert advanced.state is WorkItemState.QUALIFYING
        assert advanced.aggregate_version == 1
        assert item.state is WorkItemState.NEW

    def test_transition_to_rejects_an_illegal_transition(self) -> None:
        item = _make_work_item()

        with pytest.raises(Exception, match="illegal transition"):
            item.transition_to(WorkItemState.ACTIVE)

    def test_blocked_can_recover_to_qualifying_or_active(self) -> None:
        blocked = _make_work_item(state=WorkItemState.BLOCKED)

        assert blocked.transition_to(WorkItemState.QUALIFYING).state is WorkItemState.QUALIFYING
        assert blocked.transition_to(WorkItemState.ACTIVE).state is WorkItemState.ACTIVE
