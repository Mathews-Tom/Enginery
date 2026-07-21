"""Tests for enginery.domain.incident."""

from __future__ import annotations

import pytest

from enginery.domain.enums import RiskClass
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import IncidentId, WorkItemId
from enginery.domain.incident import (
    INCIDENT_TRANSITIONS,
    Incident,
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    severity_risk_class,
)
from tests.domain.test_state_machine import TestEveryDomainTransitionTableHasNoDeadEnds


def _make_incident(**overrides: object) -> Incident:
    defaults: dict[str, object] = {
        "id": IncidentId("inc-1"),
        "work_item_id": WorkItemId("wi-1"),
        "severity": IncidentSeverity.HIGH,
        "state": IncidentState.INTAKE,
        "summary": "checkout endpoint returns 500 for all requests",
    }
    defaults.update(overrides)
    return Incident(**defaults)  # type: ignore[arg-type]


class TestIncidentState:
    def test_has_the_fifteen_designed_states(self) -> None:
        assert {member.value for member in IncidentState} == {
            "intake",
            "classified",
            "containing",
            "reproducing",
            "remediating",
            "deploying",
            "observing",
            "rolling_back",
            "hotfix_ready",
            "mitigated",
            "resolved",
            "rolled_back",
            "blocked",
            "cancelled",
            "failed",
        }

    def test_terminal_states_match_the_design_vocabulary(self) -> None:
        assert INCIDENT_TRANSITIONS.terminal_states == {
            IncidentState.HOTFIX_READY,
            IncidentState.MITIGATED,
            IncidentState.RESOLVED,
            IncidentState.ROLLED_BACK,
            IncidentState.BLOCKED,
            IncidentState.CANCELLED,
            IncidentState.FAILED,
        }


class TestIncidentTransitionsHaveNoDeadEnds(TestEveryDomainTransitionTableHasNoDeadEnds):
    def test_no_dead_ends(self) -> None:
        self.assert_every_non_terminal_state_reaches_a_terminal(INCIDENT_TRANSITIONS)


class TestSeverityRiskClass:
    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            (IncidentSeverity.LOW, RiskClass.LOW),
            (IncidentSeverity.MEDIUM, RiskClass.MEDIUM),
            (IncidentSeverity.HIGH, RiskClass.HIGH),
            (IncidentSeverity.CRITICAL, RiskClass.HIGH),
        ],
    )
    def test_maps_severity_to_risk_class(
        self, severity: IncidentSeverity, expected: RiskClass
    ) -> None:
        assert severity_risk_class(severity) is expected

    def test_critical_is_never_lower_authority_than_high(self) -> None:
        assert severity_risk_class(IncidentSeverity.CRITICAL) is severity_risk_class(
            IncidentSeverity.HIGH
        )


class TestReleaseLineage:
    def test_valid_lineage_constructs(self) -> None:
        lineage = ReleaseLineage(
            service="checkout", affected_revision="v1", known_good_revision="v0"
        )
        assert lineage.affected_revision == "v1"

    def test_rejects_blank_service(self) -> None:
        with pytest.raises(InvalidInputError, match="service"):
            ReleaseLineage(service="  ", affected_revision="v1")

    def test_rejects_blank_affected_revision(self) -> None:
        with pytest.raises(InvalidInputError, match="affected_revision"):
            ReleaseLineage(service="checkout", affected_revision=" ")

    def test_rejects_blank_known_good_revision_when_present(self) -> None:
        with pytest.raises(InvalidInputError, match="known_good_revision"):
            ReleaseLineage(service="checkout", affected_revision="v1", known_good_revision=" ")

    def test_rejects_known_good_revision_equal_to_affected_revision(self) -> None:
        with pytest.raises(InvalidInputError, match="differ"):
            ReleaseLineage(service="checkout", affected_revision="v1", known_good_revision="v1")

    def test_known_good_revision_may_be_omitted(self) -> None:
        lineage = ReleaseLineage(service="checkout", affected_revision="v1")
        assert lineage.known_good_revision is None


class TestIncident:
    def test_rejects_blank_summary(self) -> None:
        with pytest.raises(InvalidInputError, match="summary"):
            _make_incident(summary=" ")

    def test_rejects_negative_aggregate_version(self) -> None:
        with pytest.raises(InvalidInputError, match="aggregate_version"):
            _make_incident(aggregate_version=-1)

    def test_risk_class_property_derives_from_severity(self) -> None:
        incident = _make_incident(severity=IncidentSeverity.CRITICAL)
        assert incident.risk_class is RiskClass.HIGH

    def test_transition_advances_state_and_version(self) -> None:
        incident = _make_incident(state=IncidentState.INTAKE)
        classified = incident.transition(IncidentState.CLASSIFIED)
        assert classified.state is IncidentState.CLASSIFIED
        assert classified.aggregate_version == incident.aggregate_version + 1

    def test_transition_rejects_an_illegal_edge(self) -> None:
        incident = _make_incident(state=IncidentState.INTAKE)
        with pytest.raises(InvalidInputError, match="illegal transition"):
            incident.transition(IncidentState.RESOLVED)

    def test_transition_is_immutable(self) -> None:
        incident = _make_incident(state=IncidentState.INTAKE)
        incident.transition(IncidentState.CLASSIFIED)
        assert incident.state is IncidentState.INTAKE


class TestReclassify:
    def test_reclassify_during_intake_updates_severity(self) -> None:
        incident = _make_incident(state=IncidentState.INTAKE, severity=IncidentSeverity.LOW)
        reclassified = incident.reclassify(IncidentSeverity.CRITICAL)
        assert reclassified.severity is IncidentSeverity.CRITICAL
        assert reclassified.aggregate_version == incident.aggregate_version + 1

    def test_reclassify_while_classified_updates_severity(self) -> None:
        incident = _make_incident(state=IncidentState.CLASSIFIED, severity=IncidentSeverity.LOW)
        reclassified = incident.reclassify(IncidentSeverity.HIGH)
        assert reclassified.severity is IncidentSeverity.HIGH

    def test_reclassify_after_containment_is_rejected(self) -> None:
        incident = _make_incident(state=IncidentState.CONTAINING)
        with pytest.raises(InvalidInputError, match="intake or classification"):
            incident.reclassify(IncidentSeverity.CRITICAL)


class TestBindReleaseLineage:
    def test_bind_while_classified_succeeds(self) -> None:
        incident = _make_incident(state=IncidentState.CLASSIFIED)
        lineage = ReleaseLineage(service="checkout", affected_revision="v1")
        bound = incident.bind_release_lineage(lineage)
        assert bound.release_lineage == lineage
        assert bound.aggregate_version == incident.aggregate_version + 1

    def test_bind_before_classification_is_rejected(self) -> None:
        incident = _make_incident(state=IncidentState.INTAKE)
        lineage = ReleaseLineage(service="checkout", affected_revision="v1")
        with pytest.raises(InvalidInputError, match="classified"):
            incident.bind_release_lineage(lineage)

    def test_rebinding_after_containment_is_rejected(self) -> None:
        incident = _make_incident(state=IncidentState.CONTAINING)
        lineage = ReleaseLineage(service="checkout", affected_revision="v1")
        with pytest.raises(InvalidInputError, match="classified"):
            incident.bind_release_lineage(lineage)
