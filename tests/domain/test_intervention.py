"""Tests for enginery.domain.intervention."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.ids import InterventionId, RunId
from enginery.domain.intervention import Intervention, InterventionKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_intervention(**overrides: object) -> Intervention:
    defaults: dict[str, object] = {
        "id": InterventionId("intervention-1"),
        "kind": InterventionKind.APPROVAL,
        "run_id": RunId("run-1"),
        "actor": "jane@example.com",
        "occurred_at": _NOW,
        "rationale": "approved after reviewing the diff",
    }
    defaults.update(overrides)
    return Intervention(**defaults)  # type: ignore[arg-type]


class TestInterventionKind:
    def test_has_the_seven_designed_kinds(self) -> None:
        assert {member.value for member in InterventionKind} == {
            "approval",
            "rejection",
            "correction",
            "supplied_fact",
            "waiver",
            "override",
            "manual_external_action",
        }


class TestIntervention:
    def test_constructs_with_valid_fields(self) -> None:
        intervention = _make_intervention()

        assert intervention.detail == {}

    def test_is_immutable(self) -> None:
        intervention = _make_intervention()
        with pytest.raises(AttributeError):
            intervention.actor = "other@example.com"  # type: ignore[misc]

    def test_rejects_blank_actor(self) -> None:
        with pytest.raises(Exception, match="actor"):
            _make_intervention(actor="  ")

    def test_rejects_blank_rationale(self) -> None:
        with pytest.raises(Exception, match="rationale"):
            _make_intervention(rationale="  ")

    def test_rejects_naive_occurred_at(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_intervention(occurred_at=datetime(2026, 1, 1))

    def test_detail_is_defensively_copied_from_the_caller(self) -> None:
        source = {"channel": "cli"}
        intervention = _make_intervention(detail=source)
        source["channel"] = "slack"

        assert intervention.detail["channel"] == "cli"

    def test_detail_cannot_be_mutated_through_the_instance(self) -> None:
        intervention = _make_intervention(detail={"channel": "cli"})

        with pytest.raises(TypeError):
            intervention.detail["channel"] = "slack"  # type: ignore[index]
