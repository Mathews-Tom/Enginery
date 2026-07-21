"""``IncidentService``: ingest, classify, and bind release lineage for an
incident through the shared event-sourced ledger.

An incident's own aggregate (``INCIDENT_AGGREGATE_TYPE``) and its bound
``WorkItem`` (``WORK_ITEM_AGGREGATE_TYPE``) are appended together in one
transaction at intake, so the two never diverge -- a coordinator restart
between the two writes is impossible by construction rather than
reconciled after the fact. The bound ``WorkItem`` stays at
``WorkItemState.NEW`` for the lifetime of the incident: this module never
drives it through qualification, because an incident's own
``IncidentState`` (not the generic work-item lifecycle) is the durable
lifecycle authority here. The binding exists so downstream evaluation
code (outcome capture, cohort/evaluation queries) can observe an incident
through the same ``WorkKind``-uniform pipeline every other work kind
already uses.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from enginery.application.delivery_ports import (
    DeploymentPort,
    DeploymentReceipt,
    DeploymentRequest,
    ReleaseArtifact,
)
from enginery.domain.enums import WorkKind
from enginery.domain.errors import (
    HumanActionRequiredError,
    InvalidInputError,
    MissingPrerequisiteError,
    PolicyDenialError,
)
from enginery.domain.ids import IncidentId, OperationId, RunId, WorkItemId
from enginery.domain.incident import (
    ContainmentAction,
    Incident,
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    ReproductionRecord,
    severity_risk_class,
)
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.serialization import (
    incident_from_dict,
    incident_to_dict,
    work_item_from_dict,
    work_item_to_dict,
)
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.incidents.authority import DeploymentAuthorityRecord, DeploymentGrant, issue_grant
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema

INCIDENT_AGGREGATE_TYPE = "incident"
WORK_ITEM_AGGREGATE_TYPE = "work_item"
AUTHORITY_AGGREGATE_TYPE = "incident_deployment_authority"

#: A falsifiable check executed against the affected release lineage.
#: Actually run, never a hand-typed claim -- see ``attempt_reproduction``.
ReproductionCheck = Callable[[], ReproductionRecord]


def incident_id_for(source_provider: str, external_reference: str) -> IncidentId:
    """A deterministic incident identity derived from its intake source.

    Re-ingesting the same ``(source_provider, external_reference)`` pair
    (a duplicated page, a retried webhook) resolves to the same incident
    instead of creating a second one.
    """
    payload = "\x1f".join(("incident", source_provider, external_reference))
    return IncidentId(hashlib.sha256(payload.encode("utf-8")).hexdigest())


def _incident_run_id(incident_id: IncidentId) -> RunId:
    """A deterministic ``RunId`` binding for a broker call, since incidents
    operate outside the manifest-node ``Run`` system but the delivery
    ports require one for their own operation-identity bookkeeping."""
    return RunId(f"incident:{incident_id}")


def _authority_operation_id(incident_id: IncidentId, action: str, scope: str) -> OperationId:
    payload = "\x1f".join(("incident-authority", str(incident_id), action, scope))
    return OperationId(value=hashlib.sha256(payload.encode("utf-8")).hexdigest())


def _authority_record_to_dict(record: DeploymentAuthorityRecord) -> dict[str, object]:
    grant = record.grant
    return {
        "incident_id": record.incident_id,
        "grant": {
            "grant_id": grant.grant_id,
            "action": grant.action.value,
            "target": grant.target,
            "principal_id": grant.principal_id,
            "issued_at": grant.issued_at.isoformat(),
            "expires_at": grant.expires_at.isoformat(),
        },
        "policy_decision_id": record.policy_decision_id,
        "outcome": record.outcome,
        "detail": record.detail,
    }


def _authority_record_from_dict(raw: Mapping[str, object]) -> DeploymentAuthorityRecord:
    grant_raw = raw["grant"]
    if not isinstance(grant_raw, dict):
        raise InvalidInputError("authority record grant must be a mapping")
    grant = DeploymentGrant(
        grant_id=str(grant_raw["grant_id"]),
        action=PolicyAction(grant_raw["action"]),
        target=str(grant_raw["target"]),
        principal_id=str(grant_raw["principal_id"]),
        issued_at=datetime.fromisoformat(str(grant_raw["issued_at"])),
        expires_at=datetime.fromisoformat(str(grant_raw["expires_at"])),
    )
    return DeploymentAuthorityRecord(
        incident_id=str(raw["incident_id"]),
        grant=grant,
        policy_decision_id=str(raw["policy_decision_id"]),
        outcome=str(raw["outcome"]),
        detail=str(raw["detail"]),
    )


@dataclass(frozen=True, slots=True)
class IncidentService:
    """Ingest, classify, and bind release lineage for one incident."""

    ledger: LedgerService
    deployment: DeploymentPort | None = None
    policy: PolicyEvaluator | None = None

    def ingest(
        self,
        *,
        source_provider: str,
        external_reference: str,
        source_snapshot_reference: str,
        title: str,
        objective: str,
        acceptance_criteria: tuple[str, ...],
        repository_targets: tuple[str, ...],
        severity: IncidentSeverity,
        summary: str,
        constraints: tuple[str, ...] = (),
    ) -> Incident:
        """Idempotently record one incident and its bound work item at intake.

        Re-ingesting the same source reference returns the already-recorded
        incident rather than raising or duplicating -- a paging system
        retrying a webhook delivery must never create two incidents for
        one real event.
        """
        incident_id = incident_id_for(source_provider, external_reference)
        existing = self.read(incident_id)
        if existing is not None:
            return existing
        work_item_id = WorkItemId(f"incident:{incident_id}")
        work_item = WorkItem(
            id=work_item_id,
            work_kind=WorkKind.INCIDENT,
            source_provider=source_provider,
            external_reference=external_reference,
            source_snapshot_reference=source_snapshot_reference,
            title=title,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            risk_class=severity_risk_class(severity),
            repository_targets=repository_targets,
            dependencies=(),
            state=WorkItemState.NEW,
        )
        incident = Incident(
            id=incident_id,
            work_item_id=work_item_id,
            severity=severity,
            state=IncidentState.INTAKE,
            summary=summary,
        )
        try:
            self._append_intake(incident, work_item)
        except ExpectedVersionConflictError:
            # A concurrent ingest for the same deterministic id won the
            # race; read back its result rather than duplicate it.
            resolved = self.read(incident_id)
            if resolved is None:  # pragma: no cover - defensive, ledger invariant
                raise
            return resolved
        return incident

    def classify(
        self, incident_id: IncidentId, *, severity: IncidentSeverity | None = None
    ) -> Incident:
        """Move an incident from intake to classified, optionally revising severity."""
        incident = self._require(incident_id)
        if severity is not None and severity is not incident.severity:
            incident = incident.reclassify(severity)
        if incident.state is IncidentState.INTAKE:
            incident = incident.transition(IncidentState.CLASSIFIED)
        elif incident.state is not IncidentState.CLASSIFIED:
            raise InvalidInputError(
                "an incident can only be classified from intake or while already classified",
                details={"state": incident.state.value},
            )
        self._append_incident(incident, event_type="incident.classified")
        return incident

    def bind_release_lineage(self, incident_id: IncidentId, lineage: ReleaseLineage) -> Incident:
        """Bind the affected release lineage for a classified incident."""
        incident = self._require(incident_id)
        incident = incident.bind_release_lineage(lineage)
        self._append_incident(incident, event_type="incident.release_lineage_bound")
        return incident

    def contain(self, incident_id: IncidentId, *, description: str, rationale: str) -> Incident:
        """Apply a deliberately smaller containment action for a classified incident."""
        incident = self._require(incident_id)
        action = ContainmentAction(description=description, rationale=rationale)
        incident = incident.apply_containment(action)
        self._append_incident(incident, event_type="incident.contained")
        return incident

    def resolve_containment(self, incident_id: IncidentId, *, mitigated: bool) -> Incident:
        """Resolve an in-progress containment: terminal ``mitigated``, or
        continue toward reproduction and full remediation."""
        incident = self._require(incident_id)
        target = IncidentState.MITIGATED if mitigated else IncidentState.REPRODUCING
        incident = incident.transition(target)
        event_type = "incident.mitigated" if mitigated else "incident.containment_resolved"
        self._append_incident(incident, event_type=event_type)
        return incident

    def begin_reproduction(self, incident_id: IncidentId) -> Incident:
        """Move a classified incident directly into reproduction, without containment."""
        incident = self._require(incident_id)
        incident = incident.transition(IncidentState.REPRODUCING)
        self._append_incident(incident, event_type="incident.reproduction_started")
        return incident

    def attempt_reproduction(
        self, incident_id: IncidentId, *, check: ReproductionCheck
    ) -> Incident:
        """Run a falsifiable reproduction check and record its outcome.

        ``check`` is caller-supplied and actually executed here -- a
        reproduction can only be recorded from a real observation, never
        a hand-typed claim, matching "unreproduced incidents are never
        labeled reproduced".
        """
        incident = self._require(incident_id)
        record = check()
        incident = incident.record_reproduction(record)
        self._append_incident(incident, event_type="incident.reproduction_recorded")
        return incident

    def mark_hotfix_ready(self, incident_id: IncidentId) -> Incident:
        """Move a remediated incident to ``hotfix_ready``: the repair is
        validated, reviewed, and PR-ready, but not yet deployed."""
        incident = self._require(incident_id)
        incident = incident.transition(IncidentState.HOTFIX_READY)
        self._append_incident(incident, event_type="incident.hotfix_ready")
        return incident

    def begin_deployment(self, incident_id: IncidentId) -> Incident:
        """Move a remediated incident into deployment against the controlled target."""
        incident = self._require(incident_id)
        incident = incident.transition(IncidentState.DEPLOYING)
        self._append_incident(incident, event_type="incident.deployment_started")
        return incident

    def begin_observation(self, incident_id: IncidentId) -> Incident:
        """Move a deployed incident into post-deployment observation."""
        incident = self._require(incident_id)
        incident = incident.transition(IncidentState.OBSERVING)
        self._append_incident(incident, event_type="incident.observation_started")
        return incident

    def resolve_observation(self, incident_id: IncidentId, *, healthy: bool) -> Incident:
        """Resolve an observation window.

        A healthy result resolves the incident (terminal). An unhealthy
        result begins rollback -- the actual rollback broker invocation
        and its authority record are a separate, subsequent step.
        """
        incident = self._require(incident_id)
        target = IncidentState.RESOLVED if healthy else IncidentState.ROLLING_BACK
        incident = incident.transition(target)
        event_type = "incident.resolved" if healthy else "incident.rollback_started"
        self._append_incident(incident, event_type=event_type)
        return incident

    def execute_deployment(
        self,
        incident_id: IncidentId,
        *,
        artifact: ReleaseArtifact,
        requesting_principal_id: str,
        now: datetime,
        reference_time: datetime | None = None,
    ) -> DeploymentReceipt:
        """Authorize and execute a deployment through the fixed local-service broker.

        Requires an ``ALLOW`` decision for ``deployment.execute`` -- a
        hard-required-human action (``policy/rules.py``) -- before
        issuing a short-lived grant and calling the broker. The
        incident must already be ``deploying`` (see ``begin_deployment``)
        and must already have a bound release lineage. ``reference_time``
        (defaults to ``now``) is the instant the grant is checked for
        expiry -- distinct from ``now`` so a caller can deterministically
        exercise the expired-credential-reference fault: issue at ``now``
        with a short ``ttl``, then check against a later ``reference_time``.
        """
        incident = self._require(incident_id)
        if incident.state is not IncidentState.DEPLOYING:
            raise InvalidInputError(
                "deployment can only be executed while deploying",
                details={"state": incident.state.value},
            )
        if incident.release_lineage is None:
            raise MissingPrerequisiteError(
                "incident has no bound release lineage", details={"incident_id": str(incident_id)}
            )
        target = incident.release_lineage.service
        decision = self._require_policy().evaluate(
            ApprovalSchema(
                action=PolicyAction.DEPLOYMENT_EXECUTE,
                risk_class=incident.risk_class,
                target_resource=target,
                diff_or_artifact_digest=str(artifact.digest),
                requesting_principal_id=requesting_principal_id,
            )
        )
        if decision.result is PolicyResult.DENY:
            raise PolicyDenialError(
                "policy does not permit this deployment",
                details={"policy_rule_id": decision.policy_rule_id},
            )
        if decision.result is not PolicyResult.ALLOW:
            raise HumanActionRequiredError(
                "deployment.execute requires a current, interactive human approval "
                "before any deployment to the controlled target",
                details={
                    "policy_rule_id": decision.policy_rule_id,
                    "result": decision.result.value,
                },
            )
        grant = issue_grant(
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target=target,
            principal_id=requesting_principal_id,
            issued_at=now,
        )
        grant.require_not_expired(reference_time=reference_time or now)
        receipt = self._require_deployment().deploy(
            DeploymentRequest(
                run_id=_incident_run_id(incident_id),
                artifact=artifact,
                target=target,
                operation_id=_authority_operation_id(incident_id, "deploy", str(artifact.digest)),
            )
        )
        self._append_authority_record(
            incident_id=incident_id,
            grant=grant,
            policy_decision_id=str(decision.id),
            outcome="succeeded",
            detail=f"deployed {artifact.version} to {target}",
        )
        return receipt

    def execute_rollback(
        self,
        incident_id: IncidentId,
        *,
        receipt: DeploymentReceipt,
        requesting_principal_id: str,
        now: datetime,
        reference_time: datetime | None = None,
    ) -> Incident:
        """Authorize and execute rollback through the fixed local-service broker.

        Requires an independent ``ALLOW`` decision for
        ``deployment.rollback`` -- a deployment approval never satisfies
        this, since ``ApprovalRegistry`` keys every approval on its
        exact action-bound schema digest. On a matching broker result,
        transitions the incident to its terminal ``rolled_back`` state;
        any other broker outcome transitions to ``failed`` with the
        conflict recorded as evidence rather than silently retried or
        reported as success. ``reference_time`` (defaults to ``now``) is
        the instant the grant is checked for expiry -- see
        ``execute_deployment`` for the expired-credential-reference fault
        this decoupling exists to exercise.
        """
        incident = self._require(incident_id)
        if incident.state is not IncidentState.ROLLING_BACK:
            raise InvalidInputError(
                "rollback can only be executed while rolling back",
                details={"state": incident.state.value},
            )
        target = receipt.target
        decision = self._require_policy().evaluate(
            ApprovalSchema(
                action=PolicyAction.DEPLOYMENT_ROLLBACK,
                risk_class=incident.risk_class,
                target_resource=target,
                diff_or_artifact_digest=str(receipt.artifact_digest),
                requesting_principal_id=requesting_principal_id,
            )
        )
        if decision.result is PolicyResult.DENY:
            raise PolicyDenialError(
                "policy does not permit this rollback",
                details={"policy_rule_id": decision.policy_rule_id},
            )
        if decision.result is not PolicyResult.ALLOW:
            raise HumanActionRequiredError(
                "deployment.rollback requires a current, interactive human approval "
                "before any rollback of the controlled target",
                details={
                    "policy_rule_id": decision.policy_rule_id,
                    "result": decision.result.value,
                },
            )
        grant = issue_grant(
            action=PolicyAction.DEPLOYMENT_ROLLBACK,
            target=target,
            principal_id=requesting_principal_id,
            issued_at=now,
        )
        grant.require_not_expired(reference_time=reference_time or now)
        operation_id = _authority_operation_id(
            incident_id, "rollback", str(receipt.artifact_digest)
        )
        result = self._require_deployment().rollback(receipt, operation_id=operation_id)
        if result is ReconciliationResult.FOUND_MATCHING:
            incident = incident.transition(IncidentState.ROLLED_BACK)
            outcome = "succeeded"
            event_type = "incident.rolled_back"
        else:
            incident = incident.transition(IncidentState.FAILED)
            outcome = "failed"
            event_type = "incident.rollback_failed"
        self._append_incident(incident, event_type=event_type)
        self._append_authority_record(
            incident_id=incident_id,
            grant=grant,
            policy_decision_id=str(decision.id),
            outcome=outcome,
            detail=f"rollback for target {target}: {result.value}",
        )
        return incident

    def list_authority_records(
        self, incident_id: IncidentId
    ) -> tuple[DeploymentAuthorityRecord, ...]:
        records = self.ledger.list_projections(aggregate_type=AUTHORITY_AGGREGATE_TYPE)
        all_records = tuple(_authority_record_from_dict(record.state) for record in records)
        return tuple(record for record in all_records if record.incident_id == str(incident_id))

    def _require_deployment(self) -> DeploymentPort:
        if self.deployment is None:
            raise MissingPrerequisiteError(
                "no deployment broker configured for this IncidentService"
            )
        return self.deployment

    def _require_policy(self) -> PolicyEvaluator:
        if self.policy is None:
            raise MissingPrerequisiteError(
                "no policy evaluator configured for this IncidentService"
            )
        return self.policy

    def _append_authority_record(
        self,
        *,
        incident_id: IncidentId,
        grant: DeploymentGrant,
        policy_decision_id: str,
        outcome: str,
        detail: str,
    ) -> None:
        record = DeploymentAuthorityRecord(
            incident_id=str(incident_id),
            grant=grant,
            policy_decision_id=policy_decision_id,
            outcome=outcome,
            detail=detail,
        )
        self.ledger.append(
            AppendCommand(
                correlation_id=f"incident-authority:{incident_id}:{grant.grant_id}",
                events=(
                    EventWrite(
                        aggregate_type=AUTHORITY_AGGREGATE_TYPE,
                        aggregate_id=grant.grant_id,
                        expected_version=0,
                        event_type=f"deployment_authority.{outcome}",
                        schema_version=1,
                        payload=_authority_record_to_dict(record),
                    ),
                ),
            )
        )

    def read(self, incident_id: IncidentId) -> Incident | None:
        projection = self.ledger.read_projection(
            aggregate_type=INCIDENT_AGGREGATE_TYPE, aggregate_id=str(incident_id)
        )
        if projection is None:
            return None
        return incident_from_dict(projection.state)

    def read_work_item(self, work_item_id: WorkItemId) -> WorkItem | None:
        projection = self.ledger.read_projection(
            aggregate_type=WORK_ITEM_AGGREGATE_TYPE, aggregate_id=str(work_item_id)
        )
        if projection is None:
            return None
        return work_item_from_dict(projection.state)

    def list_incidents(self, *, state: IncidentState | None = None) -> tuple[Incident, ...]:
        records = self.ledger.list_projections(aggregate_type=INCIDENT_AGGREGATE_TYPE)
        incidents = tuple(incident_from_dict(record.state) for record in records)
        if state is None:
            return incidents
        return tuple(incident for incident in incidents if incident.state is state)

    def _require(self, incident_id: IncidentId) -> Incident:
        incident = self.read(incident_id)
        if incident is None:
            raise InvalidInputError(
                "no incident is registered for this id", details={"incident_id": str(incident_id)}
            )
        return incident

    def _append_intake(self, incident: Incident, work_item: WorkItem) -> None:
        self.ledger.append(
            AppendCommand(
                correlation_id=f"incident-ingest:{incident.id}",
                events=(
                    EventWrite(
                        aggregate_type=INCIDENT_AGGREGATE_TYPE,
                        aggregate_id=str(incident.id),
                        expected_version=0,
                        event_type="incident.ingested",
                        schema_version=1,
                        payload=incident_to_dict(incident),
                    ),
                    EventWrite(
                        aggregate_type=WORK_ITEM_AGGREGATE_TYPE,
                        aggregate_id=str(work_item.id),
                        expected_version=0,
                        event_type="work_item.created",
                        schema_version=1,
                        payload=work_item_to_dict(work_item),
                    ),
                ),
            )
        )

    def _current_incident_version(self, incident_id: IncidentId) -> int:
        projection = self.ledger.read_projection(
            aggregate_type=INCIDENT_AGGREGATE_TYPE, aggregate_id=str(incident_id)
        )
        return 0 if projection is None else projection.aggregate_version

    def _append_incident(self, incident: Incident, *, event_type: str) -> None:
        expected_version = self._current_incident_version(incident.id)
        self.ledger.append(
            AppendCommand(
                correlation_id=f"incident-transition:{incident.id}:{expected_version}",
                events=(
                    EventWrite(
                        aggregate_type=INCIDENT_AGGREGATE_TYPE,
                        aggregate_id=str(incident.id),
                        expected_version=expected_version,
                        event_type=event_type,
                        schema_version=1,
                        payload=incident_to_dict(incident),
                    ),
                ),
            )
        )


__all__ = [
    "AUTHORITY_AGGREGATE_TYPE",
    "INCIDENT_AGGREGATE_TYPE",
    "WORK_ITEM_AGGREGATE_TYPE",
    "IncidentService",
    "ReproductionCheck",
    "incident_id_for",
]
