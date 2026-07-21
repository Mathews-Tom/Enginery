"""``Incident``: a production-style incident bound to affected release lineage.

Declares the aggregate, its closed lifecycle-state vocabulary, and guarded
transition enforcement built on the shared ``TransitionTable`` machinery,
following the same one-aggregate-per-workflow pattern already established
by ``WorkItem``, ``Run``, and ``FactoryChange``. An incident's terminal
vocabulary (``hotfix_ready``, ``mitigated``, ``resolved``, ``rolled_back``,
``blocked``, ``cancelled``, ``failed``) is deliberately its own closed set
rather than an extension of ``RunState``: ``RunState`` is shared by every
workflow today and carries no incident-specific meaning, exactly as
Stage 1's ``merge_ready`` and Stage 2's ``released`` are workflow-level
terminal projections layered on top of ``RunState.SUCCEEDED`` rather than
new ``RunState`` values.

Severity determines authority: ``severity_risk_class`` maps severity onto
the shared ``RiskClass`` vocabulary policy already gates on, so a critical
incident's deployment and rollback actions route through exactly the same
policy machinery every other workflow uses rather than a parallel
authority model.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from enginery.domain.enums import RiskClass
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import IncidentId, WorkItemId
from enginery.domain.state_machine import TransitionTable


class IncidentSeverity(enum.Enum):
    """The four incident severities. Severity is the sole input to authority routing."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def severity_risk_class(severity: IncidentSeverity) -> RiskClass:
    """Map incident severity onto the shared policy risk-class vocabulary.

    ``HIGH`` and ``CRITICAL`` both route to ``RiskClass.HIGH`` so both
    require human final review per design's evidence contract ("medium-
    and high-risk work requires human final review") -- a critical
    incident is never treated as lower authority than a merely high one.
    """
    if severity in (IncidentSeverity.HIGH, IncidentSeverity.CRITICAL):
        return RiskClass.HIGH
    if severity is IncidentSeverity.MEDIUM:
        return RiskClass.MEDIUM
    return RiskClass.LOW


class IncidentState(enum.Enum):
    """The fifteen incident lifecycle states."""

    INTAKE = "intake"
    CLASSIFIED = "classified"
    CONTAINING = "containing"
    REPRODUCING = "reproducing"
    REMEDIATING = "remediating"
    DEPLOYING = "deploying"
    OBSERVING = "observing"
    ROLLING_BACK = "rolling_back"
    HOTFIX_READY = "hotfix_ready"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    ROLLED_BACK = "rolled_back"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED = "failed"


INCIDENT_TRANSITIONS: TransitionTable[IncidentState] = TransitionTable(
    edges={
        IncidentState.INTAKE: frozenset(
            {IncidentState.CLASSIFIED, IncidentState.BLOCKED, IncidentState.CANCELLED}
        ),
        IncidentState.CLASSIFIED: frozenset(
            {
                IncidentState.CONTAINING,
                IncidentState.REPRODUCING,
                IncidentState.BLOCKED,
                IncidentState.CANCELLED,
            }
        ),
        IncidentState.CONTAINING: frozenset(
            {
                IncidentState.MITIGATED,
                IncidentState.REPRODUCING,
                IncidentState.BLOCKED,
                IncidentState.FAILED,
            }
        ),
        IncidentState.REPRODUCING: frozenset(
            {IncidentState.REMEDIATING, IncidentState.BLOCKED, IncidentState.FAILED}
        ),
        IncidentState.REMEDIATING: frozenset(
            {
                IncidentState.DEPLOYING,
                IncidentState.HOTFIX_READY,
                IncidentState.BLOCKED,
                IncidentState.FAILED,
            }
        ),
        IncidentState.DEPLOYING: frozenset(
            {IncidentState.OBSERVING, IncidentState.BLOCKED, IncidentState.FAILED}
        ),
        IncidentState.OBSERVING: frozenset(
            {IncidentState.RESOLVED, IncidentState.ROLLING_BACK, IncidentState.BLOCKED}
        ),
        IncidentState.ROLLING_BACK: frozenset({IncidentState.ROLLED_BACK, IncidentState.FAILED}),
    },
    terminal_states=frozenset(
        {
            IncidentState.HOTFIX_READY,
            IncidentState.MITIGATED,
            IncidentState.RESOLVED,
            IncidentState.ROLLED_BACK,
            IncidentState.BLOCKED,
            IncidentState.CANCELLED,
            IncidentState.FAILED,
        }
    ),
)


@dataclass(frozen=True, slots=True)
class ReleaseLineage:
    """The exact affected release identity a hotfix must target.

    ``affected_revision`` and ``known_good_revision`` are opaque provider
    revision identifiers (a git SHA, a fixture ``ReleaseArtifact.version``,
    or an equivalent stable label) -- this module never interprets their
    format, matching every other provider-neutral value type in this
    package.
    """

    service: str
    affected_revision: str
    known_good_revision: str | None = None

    def __post_init__(self) -> None:
        if not self.service.strip():
            raise InvalidInputError("release lineage service must be non-blank")
        if not self.affected_revision.strip():
            raise InvalidInputError("release lineage affected_revision must be non-blank")
        if self.known_good_revision is not None and not self.known_good_revision.strip():
            raise InvalidInputError(
                "release lineage known_good_revision, when present, must be non-blank"
            )
        if self.known_good_revision == self.affected_revision:
            raise InvalidInputError(
                "release lineage known_good_revision must differ from affected_revision"
            )


@dataclass(frozen=True, slots=True)
class ContainmentAction:
    """A deliberately smaller mitigating action, distinct from a code fix.

    Containment (a feature flag, a traffic throttle, a rate limit) buys
    time or limits blast radius without touching the affected service's
    code -- this module never conflates it with remediation.
    """

    description: str
    rationale: str

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise InvalidInputError("containment action description must be non-blank")
        if not self.rationale.strip():
            raise InvalidInputError("containment action rationale must be non-blank")


class ReproductionOutcome(enum.Enum):
    """The three closed outcomes of a falsifiable reproduction attempt."""

    REPRODUCED = "reproduced"
    UNAVAILABLE = "unavailable"
    ERRORED = "errored"


@dataclass(frozen=True, slots=True)
class ReproductionRecord:
    """The result of one falsifiable reproduction attempt against the
    affected release lineage. ``detail`` must describe exactly what was
    observed -- a bare claim of ``REPRODUCED`` with no observation is
    exactly what design's "never labeled reproduced" acceptance forbids.
    """

    outcome: ReproductionOutcome
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise InvalidInputError("reproduction record detail must be non-blank")


@dataclass(frozen=True, slots=True)
class Incident:
    """A production-style incident bound to a work item and release lineage."""

    id: IncidentId
    work_item_id: WorkItemId
    severity: IncidentSeverity
    state: IncidentState
    summary: str
    release_lineage: ReleaseLineage | None = None
    containment: ContainmentAction | None = None
    reproduction: ReproductionRecord | None = None
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise InvalidInputError("incident summary must be non-blank")
        if self.aggregate_version < 0:
            raise InvalidInputError("incident aggregate_version cannot be negative")

    @property
    def risk_class(self) -> RiskClass:
        return severity_risk_class(self.severity)

    def bind_release_lineage(self, lineage: ReleaseLineage) -> Incident:
        """Bind the affected release lineage. Only valid while ``CLASSIFIED``.

        Rebinding from a later working state is intentionally rejected: a
        hotfix workspace built against one lineage must never be silently
        retargeted to another mid-flight.
        """
        if self.state is not IncidentState.CLASSIFIED:
            raise InvalidInputError(
                "release lineage can only be bound while an incident is classified",
                details={"state": self.state.value},
            )
        return replace(self, release_lineage=lineage, aggregate_version=self.aggregate_version + 1)

    def apply_containment(self, action: ContainmentAction) -> Incident:
        """Record a containment action and move to ``CONTAINING``.

        Only valid from ``CLASSIFIED``: containment is a deliberate,
        smaller step distinct from remediation, matching design's
        "separates containment from remediation".
        """
        if self.state is not IncidentState.CLASSIFIED:
            raise InvalidInputError(
                "containment can only be applied while an incident is classified",
                details={"state": self.state.value},
            )
        return replace(
            self,
            containment=action,
            state=IncidentState.CONTAINING,
            aggregate_version=self.aggregate_version + 1,
        )

    def record_reproduction(self, record: ReproductionRecord) -> Incident:
        """Record a falsifiable reproduction attempt and route the outcome.

        Only valid from ``REPRODUCING``. ``REPRODUCED`` proceeds to
        remediation; ``UNAVAILABLE`` routes to ``BLOCKED`` so an
        unreproduced incident is visible rather than silently treated as
        confirmed; ``ERRORED`` routes to ``FAILED``.
        """
        if self.state is not IncidentState.REPRODUCING:
            raise InvalidInputError(
                "reproduction can only be recorded while reproducing",
                details={"state": self.state.value},
            )
        target = {
            ReproductionOutcome.REPRODUCED: IncidentState.REMEDIATING,
            ReproductionOutcome.UNAVAILABLE: IncidentState.BLOCKED,
            ReproductionOutcome.ERRORED: IncidentState.FAILED,
        }[record.outcome]
        INCIDENT_TRANSITIONS.require(self.state, target)
        return replace(
            self,
            reproduction=record,
            state=target,
            aggregate_version=self.aggregate_version + 1,
        )

    def reclassify(self, severity: IncidentSeverity) -> Incident:
        """Revise severity while intake or classification is still open.

        Rejected once containment, reproduction, or remediation has
        started: the incident workflow "freezes available evidence" at
        classification time, so authority cannot silently shift under an
        in-progress remediation.
        """
        if self.state not in (IncidentState.INTAKE, IncidentState.CLASSIFIED):
            raise InvalidInputError(
                "severity can only be revised during intake or classification",
                details={"state": self.state.value},
            )
        return replace(self, severity=severity, aggregate_version=self.aggregate_version + 1)

    def transition(self, target: IncidentState) -> Incident:
        INCIDENT_TRANSITIONS.require(self.state, target)
        return replace(self, state=target, aggregate_version=self.aggregate_version + 1)


__all__ = [
    "INCIDENT_TRANSITIONS",
    "ContainmentAction",
    "Incident",
    "IncidentSeverity",
    "IncidentState",
    "ReleaseLineage",
    "ReproductionOutcome",
    "ReproductionRecord",
    "severity_risk_class",
]
