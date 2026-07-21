"""Tests for enginery.incidents.service.IncidentService."""

from __future__ import annotations

import pytest

from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import IncidentId
from enginery.domain.incident import (
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    ReproductionOutcome,
    ReproductionRecord,
)
from enginery.domain.work_item import WorkItemState
from enginery.incidents.service import IncidentService, incident_id_for
from enginery.ledger.service import LedgerService

_SOURCE_PROVIDER = "pagerduty"
_EXTERNAL_REFERENCE = "PD-1001"


def _ingest(service: IncidentService, **overrides: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "source_provider": _SOURCE_PROVIDER,
        "external_reference": _EXTERNAL_REFERENCE,
        "source_snapshot_reference": "snapshot-1",
        "title": "checkout returns 500",
        "objective": "restore checkout availability",
        "acceptance_criteria": ("checkout responds 200",),
        "repository_targets": ("org/checkout",),
        "severity": IncidentSeverity.HIGH,
        "summary": "checkout endpoint returns 500 for all requests",
    }
    defaults.update(overrides)
    return service.ingest(**defaults)  # type: ignore[arg-type]


class TestIncidentIdFor:
    def test_is_deterministic(self) -> None:
        first = incident_id_for(_SOURCE_PROVIDER, _EXTERNAL_REFERENCE)
        second = incident_id_for(_SOURCE_PROVIDER, _EXTERNAL_REFERENCE)
        assert first == second

    def test_differs_by_reference(self) -> None:
        first = incident_id_for(_SOURCE_PROVIDER, _EXTERNAL_REFERENCE)
        second = incident_id_for(_SOURCE_PROVIDER, "PD-1002")
        assert first != second


class TestIngest:
    def test_ingests_a_new_incident_at_intake(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)

        incident = _ingest(service)

        assert incident.state is IncidentState.INTAKE
        assert incident.severity is IncidentSeverity.HIGH
        assert service.read(incident.id) == incident

    def test_binds_a_work_item_with_the_incident_work_kind(
        self, ledger_service: LedgerService
    ) -> None:
        service = IncidentService(ledger=ledger_service)

        incident = _ingest(service)
        work_item = service.read_work_item(incident.work_item_id)

        assert work_item is not None
        assert work_item.work_kind is WorkKind.INCIDENT
        assert work_item.state is WorkItemState.NEW
        assert work_item.risk_class is RiskClass.HIGH

    def test_re_ingesting_the_same_reference_is_idempotent(
        self, ledger_service: LedgerService
    ) -> None:
        service = IncidentService(ledger=ledger_service)

        first = _ingest(service)
        second = _ingest(service, severity=IncidentSeverity.LOW)

        assert first.id == second.id
        assert second.severity is IncidentSeverity.HIGH
        assert len(service.list_incidents()) == 1


class TestClassify:
    def test_classifies_from_intake(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)

        classified = service.classify(incident.id)

        assert classified.state is IncidentState.CLASSIFIED
        assert service.read(incident.id) == classified

    def test_classify_can_revise_severity(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service, severity=IncidentSeverity.LOW)

        classified = service.classify(incident.id, severity=IncidentSeverity.CRITICAL)

        assert classified.severity is IncidentSeverity.CRITICAL
        assert classified.state is IncidentState.CLASSIFIED

    def test_reclassifying_while_already_classified_stays_classified(
        self, ledger_service: LedgerService
    ) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service, severity=IncidentSeverity.LOW)
        service.classify(incident.id)

        reclassified = service.classify(incident.id, severity=IncidentSeverity.HIGH)

        assert reclassified.state is IncidentState.CLASSIFIED
        assert reclassified.severity is IncidentSeverity.HIGH

    def test_classify_unknown_incident_raises(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)

        with pytest.raises(InvalidInputError, match="no incident"):
            service.classify(IncidentId("missing"))


class TestBindReleaseLineage:
    def test_binds_lineage_after_classification(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        lineage = ReleaseLineage(
            service="checkout", affected_revision="v1", known_good_revision="v0"
        )

        bound = service.bind_release_lineage(incident.id, lineage)

        assert bound.release_lineage == lineage
        assert service.read(incident.id) == bound

    def test_binding_before_classification_is_rejected(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        lineage = ReleaseLineage(service="checkout", affected_revision="v1")

        with pytest.raises(InvalidInputError, match="classified"):
            service.bind_release_lineage(incident.id, lineage)


class TestListIncidents:
    def test_filters_by_state(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        classified_incident = _ingest(service)
        service.classify(classified_incident.id)
        _ingest(service, external_reference="PD-1002")

        classified_only = service.list_incidents(state=IncidentState.CLASSIFIED)
        intake_only = service.list_incidents(state=IncidentState.INTAKE)

        assert {incident.id for incident in classified_only} == {classified_incident.id}
        assert len(intake_only) == 1
        assert len(service.list_incidents()) == 2


class TestContain:
    def test_applies_containment_from_classified(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)

        contained = service.contain(
            incident.id, description="disable checkout", rationale="stop the bleeding"
        )

        assert contained.state is IncidentState.CONTAINING
        assert contained.containment is not None
        assert contained.containment.description == "disable checkout"
        assert service.read(incident.id) == contained


class TestResolveContainment:
    def test_mitigated_is_terminal(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        service.contain(incident.id, description="disable checkout", rationale="stop bleeding")

        resolved = service.resolve_containment(incident.id, mitigated=True)

        assert resolved.state is IncidentState.MITIGATED

    def test_not_mitigated_proceeds_to_reproducing(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        service.contain(incident.id, description="disable checkout", rationale="stop bleeding")

        resolved = service.resolve_containment(incident.id, mitigated=False)

        assert resolved.state is IncidentState.REPRODUCING


class TestBeginReproduction:
    def test_moves_directly_from_classified(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)

        reproducing = service.begin_reproduction(incident.id)

        assert reproducing.state is IncidentState.REPRODUCING


class TestAttemptReproduction:
    def test_reproduced_check_advances_to_remediating(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        service.begin_reproduction(incident.id)

        result = service.attempt_reproduction(
            incident.id,
            check=lambda: ReproductionRecord(
                outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
            ),
        )

        assert result.state is IncidentState.REMEDIATING
        assert result.reproduction is not None
        assert result.reproduction.outcome is ReproductionOutcome.REPRODUCED
        assert service.read(incident.id) == result

    def test_unavailable_check_is_visible_not_reproduced(
        self, ledger_service: LedgerService
    ) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        service.begin_reproduction(incident.id)

        result = service.attempt_reproduction(
            incident.id,
            check=lambda: ReproductionRecord(
                outcome=ReproductionOutcome.UNAVAILABLE, detail="could not reproduce against v1"
            ),
        )

        assert result.state is IncidentState.BLOCKED

    def test_check_is_actually_invoked(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        service.begin_reproduction(incident.id)
        calls: list[bool] = []

        def check() -> ReproductionRecord:
            calls.append(True)
            return ReproductionRecord(
                outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
            )

        service.attempt_reproduction(incident.id, check=check)

        assert calls == [True]


class TestMarkHotfixReady:
    def test_moves_from_remediating_to_hotfix_ready(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        service.classify(incident.id)
        service.begin_reproduction(incident.id)
        service.attempt_reproduction(
            incident.id,
            check=lambda: ReproductionRecord(
                outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
            ),
        )

        ready = service.mark_hotfix_ready(incident.id)

        assert ready.state is IncidentState.HOTFIX_READY
        assert service.read(incident.id) == ready


def _reach_remediating(service: IncidentService, incident_id: IncidentId) -> None:
    service.classify(incident_id)
    service.begin_reproduction(incident_id)
    service.attempt_reproduction(
        incident_id,
        check=lambda: ReproductionRecord(
            outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
        ),
    )


class TestBeginDeployment:
    def test_moves_from_remediating_to_deploying(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        _reach_remediating(service, incident.id)

        deploying = service.begin_deployment(incident.id)

        assert deploying.state is IncidentState.DEPLOYING
        assert service.read(incident.id) == deploying


class TestBeginObservation:
    def test_moves_from_deploying_to_observing(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        _reach_remediating(service, incident.id)
        service.begin_deployment(incident.id)

        observing = service.begin_observation(incident.id)

        assert observing.state is IncidentState.OBSERVING


class TestResolveObservation:
    def test_healthy_resolves_the_incident(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        _reach_remediating(service, incident.id)
        service.begin_deployment(incident.id)
        service.begin_observation(incident.id)

        resolved = service.resolve_observation(incident.id, healthy=True)

        assert resolved.state is IncidentState.RESOLVED

    def test_unhealthy_begins_rollback(self, ledger_service: LedgerService) -> None:
        service = IncidentService(ledger=ledger_service)
        incident = _ingest(service)
        _reach_remediating(service, incident.id)
        service.begin_deployment(incident.id)
        service.begin_observation(incident.id)

        rolling_back = service.resolve_observation(incident.id, healthy=False)

        assert rolling_back.state is IncidentState.ROLLING_BACK
