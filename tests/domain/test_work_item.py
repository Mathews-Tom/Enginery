"""Tests for enginery.domain.work_item."""

from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.ids import WorkItemId
from enginery.domain.work_item import WorkItem, WorkItemState


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
