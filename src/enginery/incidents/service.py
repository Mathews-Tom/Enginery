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
from collections.abc import Callable
from dataclasses import dataclass

from enginery.domain.enums import WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import IncidentId, WorkItemId
from enginery.domain.incident import (
    ContainmentAction,
    Incident,
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    ReproductionRecord,
    severity_risk_class,
)
from enginery.domain.serialization import (
    incident_from_dict,
    incident_to_dict,
    work_item_from_dict,
    work_item_to_dict,
)
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService

INCIDENT_AGGREGATE_TYPE = "incident"
WORK_ITEM_AGGREGATE_TYPE = "work_item"

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


@dataclass(frozen=True, slots=True)
class IncidentService:
    """Ingest, classify, and bind release lineage for one incident."""

    ledger: LedgerService

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
    "INCIDENT_AGGREGATE_TYPE",
    "WORK_ITEM_AGGREGATE_TYPE",
    "IncidentService",
    "ReproductionCheck",
    "incident_id_for",
]
