"""Tests for enginery.incidents.service.IncidentService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import (
    DeploymentReceipt,
    DeploymentRequest,
    ReleaseArtifact,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import (
    HumanActionRequiredError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.domain.ids import IncidentId, OperationId, RunId
from enginery.domain.incident import (
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    ReproductionOutcome,
    ReproductionRecord,
)
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.domain.work_item import WorkItemState
from enginery.incidents.authority import DeploymentGrantExpiredError
from enginery.incidents.service import IncidentService, incident_id_for
from enginery.ledger.service import LedgerService
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema

_HUMAN = AuthorityPrincipal(
    id="human-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_REQUESTING_PRINCIPAL_ID = "incident-workflow"
_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

_SOURCE_PROVIDER = "pagerduty"
_EXTERNAL_REFERENCE = "PD-1001"


def _approve(
    registry: ApprovalRegistry, *, action: PolicyAction, target: str, artifact_digest: Digest
) -> None:
    schema = ApprovalSchema(
        action=action,
        risk_class=RiskClass.HIGH,
        target_resource=target,
        diff_or_artifact_digest=str(artifact_digest),
        requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
    )
    registry.record_approval(schema, (_HUMAN,), decided_at=_NOW)


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


class _FakeDeployment:
    """A minimal structural DeploymentPort double: deploy/rollback are used."""

    def __init__(
        self, *, rollback_result: ReconciliationResult = ReconciliationResult.FOUND_MATCHING
    ) -> None:
        self.deploy_calls: list[DeploymentRequest] = []
        self.rollback_calls: list[DeploymentReceipt] = []
        self.rollback_result = rollback_result

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused
        raise NotImplementedError

    def deploy(self, request: DeploymentRequest) -> DeploymentReceipt:
        self.deploy_calls.append(request)
        return DeploymentReceipt(
            target=request.target,
            artifact_digest=request.artifact.digest,
            deployment_id=f"deployment-{request.operation_id}",
        )

    def rollback(
        self, receipt: DeploymentReceipt, *, operation_id: OperationId
    ) -> ReconciliationResult:
        self.rollback_calls.append(receipt)
        return self.rollback_result

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:  # pragma: no cover
        raise NotImplementedError


def _policy(*, registry: ApprovalRegistry | None = None) -> PolicyEvaluator:
    return PolicyEvaluator(policy_version="1.0.0", approval_registry=registry)


def _artifact() -> ReleaseArtifact:
    return ReleaseArtifact(
        version="v2", digest=Digest.of_bytes(b"v2-config"), media_type="application/json"
    )


_TARGET = "127.0.0.1:8765"


def _reach_deploying(
    service: IncidentService, incident_id: IncidentId, *, target: str = _TARGET
) -> ReleaseLineage:
    service.classify(incident_id)
    lineage = ReleaseLineage(service=target, affected_revision="v1")
    service.bind_release_lineage(incident_id, lineage)
    service.begin_reproduction(incident_id)
    service.attempt_reproduction(
        incident_id,
        check=lambda: ReproductionRecord(
            outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
        ),
    )
    service.begin_deployment(incident_id)
    return lineage


class TestExecuteDeployment:
    def test_requires_a_bound_release_lineage(self, ledger_service: LedgerService) -> None:
        deployment = _FakeDeployment()
        service = IncidentService(ledger=ledger_service, deployment=deployment, policy=_policy())
        incident = _ingest(service)
        _reach_remediating(service, incident.id)
        service.begin_deployment(incident.id)

        with pytest.raises(MissingPrerequisiteError, match="release lineage"):
            service.execute_deployment(
                incident.id,
                artifact=_artifact(),
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=_NOW,
            )

    def test_raises_without_a_recorded_human_approval(self, ledger_service: LedgerService) -> None:
        deployment = _FakeDeployment()
        service = IncidentService(ledger=ledger_service, deployment=deployment, policy=_policy())
        incident = _ingest(service)
        _reach_deploying(service, incident.id)

        with pytest.raises(HumanActionRequiredError):
            service.execute_deployment(
                incident.id,
                artifact=_artifact(),
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=_NOW,
            )
        assert deployment.deploy_calls == []

    def test_succeeds_after_a_recorded_human_approval(self, ledger_service: LedgerService) -> None:
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        deployment = _FakeDeployment()
        service = IncidentService(
            ledger=ledger_service, deployment=deployment, policy=_policy(registry=registry)
        )
        incident = _ingest(service)
        lineage = _reach_deploying(service, incident.id)
        artifact = _artifact()
        _approve(
            registry,
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target=lineage.service,
            artifact_digest=artifact.digest,
        )

        receipt = service.execute_deployment(
            incident.id,
            artifact=artifact,
            requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
            now=_NOW,
        )

        assert receipt.target == _TARGET
        assert len(deployment.deploy_calls) == 1
        assert deployment.deploy_calls[0].target == _TARGET
        records = service.list_authority_records(incident.id)
        assert len(records) == 1
        assert records[0].outcome == "succeeded"
        assert records[0].grant.action is PolicyAction.DEPLOYMENT_EXECUTE

    def test_expired_grant_raises_even_after_approval(self, ledger_service: LedgerService) -> None:
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        deployment = _FakeDeployment()
        service = IncidentService(
            ledger=ledger_service, deployment=deployment, policy=_policy(registry=registry)
        )
        incident = _ingest(service)
        lineage = _reach_deploying(service, incident.id)
        artifact = _artifact()
        _approve(
            registry,
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target=lineage.service,
            artifact_digest=artifact.digest,
        )

        with pytest.raises(DeploymentGrantExpiredError):
            service.execute_deployment(
                incident.id,
                artifact=artifact,
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=_NOW,
                reference_time=_NOW + timedelta(minutes=10),
            )
        assert deployment.deploy_calls == []


def _reach_rolling_back(
    service: IncidentService, incident_id: IncidentId, *, deployment: _FakeDeployment
) -> tuple[ReleaseLineage, DeploymentReceipt]:
    lineage = _reach_deploying(service, incident_id)
    receipt = deployment.deploy(
        DeploymentRequest(
            run_id=RunId(f"incident:{incident_id}"),
            artifact=_artifact(),
            target=lineage.service,
            operation_id=OperationId(value="a" * 64),
        )
    )
    service.begin_observation(incident_id)
    service.resolve_observation(incident_id, healthy=False)
    return lineage, receipt


class TestExecuteRollback:
    def test_raises_without_a_recorded_human_approval(self, ledger_service: LedgerService) -> None:
        deployment = _FakeDeployment()
        service = IncidentService(ledger=ledger_service, deployment=deployment, policy=_policy())
        incident = _ingest(service)
        _, receipt = _reach_rolling_back(service, incident.id, deployment=deployment)

        with pytest.raises(HumanActionRequiredError):
            service.execute_rollback(
                incident.id,
                receipt=receipt,
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=_NOW,
            )
        assert deployment.rollback_calls == []

    def test_a_deployment_approval_does_not_satisfy_rollback(
        self, ledger_service: LedgerService
    ) -> None:
        """Deployment and rollback approvals are independently keyed on
        their own action -- proving design's "separately authorized"."""
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        deployment = _FakeDeployment()
        service = IncidentService(
            ledger=ledger_service, deployment=deployment, policy=_policy(registry=registry)
        )
        incident = _ingest(service)
        lineage, receipt = _reach_rolling_back(service, incident.id, deployment=deployment)
        _approve(
            registry,
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target=lineage.service,
            artifact_digest=receipt.artifact_digest,
        )

        with pytest.raises(HumanActionRequiredError):
            service.execute_rollback(
                incident.id,
                receipt=receipt,
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=_NOW,
            )

    def test_succeeds_after_a_recorded_rollback_approval(
        self, ledger_service: LedgerService
    ) -> None:
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        deployment = _FakeDeployment()
        service = IncidentService(
            ledger=ledger_service, deployment=deployment, policy=_policy(registry=registry)
        )
        incident = _ingest(service)
        lineage, receipt = _reach_rolling_back(service, incident.id, deployment=deployment)
        _approve(
            registry,
            action=PolicyAction.DEPLOYMENT_ROLLBACK,
            target=lineage.service,
            artifact_digest=receipt.artifact_digest,
        )

        resolved = service.execute_rollback(
            incident.id,
            receipt=receipt,
            requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
            now=_NOW,
        )

        assert resolved.state is IncidentState.ROLLED_BACK
        assert len(deployment.rollback_calls) == 1
        records = service.list_authority_records(incident.id)
        assert any(record.outcome == "succeeded" for record in records)

    def test_a_conflicting_broker_result_fails_rather_than_claims_success(
        self, ledger_service: LedgerService
    ) -> None:
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        deployment = _FakeDeployment(rollback_result=ReconciliationResult.FOUND_CONFLICTING)
        service = IncidentService(
            ledger=ledger_service, deployment=deployment, policy=_policy(registry=registry)
        )
        incident = _ingest(service)
        lineage, receipt = _reach_rolling_back(service, incident.id, deployment=deployment)
        _approve(
            registry,
            action=PolicyAction.DEPLOYMENT_ROLLBACK,
            target=lineage.service,
            artifact_digest=receipt.artifact_digest,
        )

        resolved = service.execute_rollback(
            incident.id,
            receipt=receipt,
            requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
            now=_NOW,
        )

        assert resolved.state is IncidentState.FAILED
        records = service.list_authority_records(incident.id)
        assert any(record.outcome == "failed" for record in records)
