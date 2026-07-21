"""``OutcomeCaptureService``: register, resolve, and query raw Stage-1
outcome observations.

Stage 1 stops at merge readiness -- it never knows whether a PR eventually
merges or gets closed unmerged. This service is the pipeline that watches
that subject after Stage 1 stops: one durable
:class:`~enginery.domain.observation.ObservationRequest` per watched
subject records a declared window; :meth:`OutcomeCaptureService.sweep`
polls the existing pull-request adapter and resolves a pending observation
to ``captured`` (linking a new, immutable
:class:`~enginery.domain.outcome.Outcome`) once the PR's real-world fate is
known. A pending observation whose window elapses unobserved becomes
``indeterminate`` -- never favorable, and never silently dropped from the
completeness accounting.

Only ``MERGE_RESULT`` is auto-swept: the pull-request port exposes a live
``state``/``merged`` read for exactly that subject. Neither a reopened
issue nor an escaped defect has an adapter signal today --
``WorkLedgerPort.fetch`` is a re-ingestion entrypoint that always returns a
fresh ``WorkItemState.NEW`` snapshot, not a live open/closed read, and this
codebase has no defect-tracking port at all. Both kinds are captured only
through an explicit, human-supplied :meth:`OutcomeCaptureService.capture`
call; a registered observation for either still counts toward
completeness and still expires to ``indeterminate`` if nobody supplies one
before its window elapses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from enginery.application.work_ports import PullRequestPort, PullRequestSnapshot
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ObservationId, OutcomeId, RunId, WorkItemId
from enginery.domain.observation import ObservationRequest, ObservationState
from enginery.domain.outcome import Outcome, OutcomeKind
from enginery.domain.serialization import (
    observation_from_dict,
    observation_to_dict,
    outcome_from_dict,
    outcome_to_dict,
)
from enginery.ledger.errors import ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService

OBSERVATION_AGGREGATE_TYPE = "observation"
OUTCOME_AGGREGATE_TYPE = "outcome"

#: Windows are deliberately per-kind: a merge/close decision is usually
#: settled within days, while a reopened issue can surface months later.
#: A kind absent from this table falls back to ``DEFAULT_WINDOW``.
DEFAULT_WINDOWS: dict[OutcomeKind, timedelta] = {
    OutcomeKind.MERGE_RESULT: timedelta(days=14),
    OutcomeKind.REOPENED_ISSUE: timedelta(days=90),
    OutcomeKind.ESCAPED_DEFECT: timedelta(days=90),
}
DEFAULT_WINDOW = timedelta(days=30)

#: See the module docstring: only a subject with a live adapter read is
#: swept automatically. Everything else requires an explicit capture.
_AUTO_SWEEPABLE_KINDS = frozenset({OutcomeKind.MERGE_RESULT})

#: Bumped whenever the completeness formula itself changes. Raw
#: observation records are immutable and never rewritten; only this
#: derivation's *interpretation* of them can gain a new version.
COMPLETENESS_DERIVATION_VERSION = 1


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    """A versioned snapshot of outcome-capture completeness."""

    derivation_version: int
    captured: int
    indeterminate: int
    pending: int
    completeness: float


def compute_completeness(
    observations: Iterable[ObservationRequest], *, reference_time: datetime
) -> CompletenessReport:
    """``captured / (captured + indeterminate)``, counted only over
    observations whose fate is already decided. A still-``pending``
    observation inside its open window is excluded from both terms --
    never counted as capture, never counted as a gap -- so it cannot
    inflate completeness. A pending observation whose window has already
    elapsed but has not yet been swept by :meth:`OutcomeCaptureService.expire_overdue`
    is likewise excluded until it is actually resolved: this keeps the
    report a read of durably recorded fact, never a projection of what
    *should* have happened by ``reference_time``. Suppressing a capture
    therefore can only ever lower this ratio once its window elapses and
    is swept -- never raise it, satisfying the anti-gaming requirement by
    construction rather than a special case."""
    captured = indeterminate = pending = 0
    for observation in observations:
        if observation.state is ObservationState.CAPTURED:
            captured += 1
        elif observation.state is ObservationState.INDETERMINATE:
            indeterminate += 1
        else:
            pending += 1
    del reference_time  # reserved for a future time-windowed derivation version
    denominator = captured + indeterminate
    completeness = 1.0 if denominator == 0 else captured / denominator
    return CompletenessReport(
        derivation_version=COMPLETENESS_DERIVATION_VERSION,
        captured=captured,
        indeterminate=indeterminate,
        pending=pending,
        completeness=completeness,
    )


def observation_id_for(run_id: RunId, kind: OutcomeKind) -> ObservationId:
    """The deterministic identity of one run's observation for one watched
    kind. Registration is idempotent by construction: re-deriving the same
    ``(run_id, kind)`` pair always yields the same :class:`ObservationId`,
    so re-registering after a coordinator restart never creates a
    duplicate observation."""
    return ObservationId(f"{run_id}:{kind.value}")


def classify_pull_request_outcome(snapshot: PullRequestSnapshot) -> OutcomeKind | None:
    """Classify a polled pull-request snapshot into a Stage-1 merge
    outcome, or ``None`` while the PR is still open (nothing to capture
    yet). ``PR_REJECTED`` is not derived here: the adapter surface exposes
    no signal distinguishing an explicit rejection from abandonment, so an
    automatically swept closed-unmerged PR is conservatively classified as
    ``PR_ABANDONED``; a human with fuller context may still record
    ``PR_REJECTED`` through an explicit capture."""
    if snapshot.merged:
        return OutcomeKind.PR_ACCEPTED
    if snapshot.state == "closed":
        return OutcomeKind.PR_ABANDONED
    return None


@dataclass(frozen=True, slots=True)
class OutcomeCaptureService:
    """Register, resolve, and read observation/outcome records through the
    shared event-sourced ledger. Read-only against the pull-request port it
    is given; it never mutates a provider subject."""

    ledger: LedgerService
    pull_requests: PullRequestPort | None = None

    def register_pending(
        self,
        *,
        work_item_id: WorkItemId,
        run_id: RunId,
        kind: OutcomeKind,
        subject_reference: str,
        opened_at: datetime,
        window: timedelta | None = None,
    ) -> ObservationRequest:
        """Idempotently register one pending observation. Re-registering
        the same ``(run_id, kind)`` pair is a no-op that returns the
        already-registered record rather than raising or duplicating."""
        observation_id = observation_id_for(run_id, kind)
        existing = self.read_observation(observation_id)
        if existing is not None:
            return existing
        observation = ObservationRequest(
            id=observation_id,
            work_item_id=work_item_id,
            run_id=run_id,
            kind=kind,
            opened_at=opened_at,
            window=window if window is not None else DEFAULT_WINDOWS.get(kind, DEFAULT_WINDOW),
            detail={"subject_reference": subject_reference},
        )
        try:
            self._append_observation(observation, expected_version=0)
        except ExpectedVersionConflictError:
            # A concurrent registration for the same deterministic id won
            # the race; read back its result rather than duplicate it.
            resolved = self.read_observation(observation_id)
            if resolved is None:  # pragma: no cover - defensive, ledger invariant
                raise
            return resolved
        return observation

    def capture(
        self,
        observation_id: ObservationId,
        *,
        outcome_id: OutcomeId,
        kind: OutcomeKind,
        observed_at: datetime,
        detail: dict[str, object] | None = None,
        linked_work_item_id: WorkItemId | None = None,
    ) -> Outcome:
        """Resolve a pending observation to ``captured``, atomically
        recording the new, immutable :class:`Outcome` it observed."""
        observation = self._require_observation(observation_id)
        outcome = Outcome(
            id=outcome_id,
            work_item_id=observation.work_item_id,
            kind=kind,
            observed_at=observed_at,
            run_id=observation.run_id,
            linked_work_item_id=linked_work_item_id,
            detail=detail or {},
        )
        resolved = observation.resolve_captured(outcome_id=outcome_id, resolved_at=observed_at)
        self._append_capture(resolved, outcome)
        return outcome

    def sweep(self, *, reference_time: datetime) -> tuple[Outcome, ...]:
        """Poll every auto-sweepable pending observation against the
        pull-request adapter and capture whatever has resolved. Returns
        every newly captured outcome; a still-open PR or a missing adapter
        leaves its observation pending."""
        captured: list[Outcome] = []
        for observation in self.list_observations(state=ObservationState.PENDING):
            if observation.kind not in _AUTO_SWEEPABLE_KINDS:
                continue
            resolved_outcome = self._sweep_merge_result(observation, reference_time=reference_time)
            if resolved_outcome is not None:
                captured.append(resolved_outcome)
        return tuple(captured)

    def expire_overdue(self, *, reference_time: datetime) -> tuple[ObservationRequest, ...]:
        """Resolve every pending observation whose declared window has
        elapsed to ``indeterminate``. An observation resolved this way
        never becomes ``captured`` later -- a late real-world signal opens
        a fresh :class:`ObservationRequest` instead of rewriting history."""
        expired: list[ObservationRequest] = []
        for observation in self.list_observations(state=ObservationState.PENDING):
            if not observation.is_overdue(reference_time=reference_time):
                continue
            resolved = observation.resolve_indeterminate(resolved_at=reference_time)
            self._append_expiry(resolved)
            expired.append(resolved)
        return tuple(expired)

    def completeness(self, *, reference_time: datetime) -> CompletenessReport:
        """The current, versioned outcome-capture completeness derivation
        over every registered observation. See :func:`compute_completeness`
        for the formula and its anti-gaming rationale."""
        return compute_completeness(self.list_observations(), reference_time=reference_time)

    def _sweep_merge_result(
        self, observation: ObservationRequest, *, reference_time: datetime
    ) -> Outcome | None:
        subject_reference = str(observation.detail.get("subject_reference", ""))
        if self.pull_requests is None or not subject_reference.isdigit():
            return None
        snapshot = self.pull_requests.get(int(subject_reference))
        resolved_kind = classify_pull_request_outcome(snapshot)
        if resolved_kind is None:
            return None
        return self.capture(
            observation.id,
            outcome_id=OutcomeId(f"{observation.run_id}:{resolved_kind.value}"),
            kind=resolved_kind,
            observed_at=reference_time,
            detail={
                "pull_request_number": snapshot.number,
                "pull_request_state": snapshot.state,
            },
        )

    def read_observation(self, observation_id: ObservationId) -> ObservationRequest | None:
        projection = self.ledger.read_projection(
            aggregate_type=OBSERVATION_AGGREGATE_TYPE, aggregate_id=str(observation_id)
        )
        if projection is None:
            return None
        return observation_from_dict(projection.state)

    def _append_expiry(self, resolved: ObservationRequest) -> None:
        self.ledger.append(
            AppendCommand(
                correlation_id=f"observation-expire:{resolved.id}",
                events=(
                    EventWrite(
                        aggregate_type=OBSERVATION_AGGREGATE_TYPE,
                        aggregate_id=str(resolved.id),
                        expected_version=1,
                        event_type="observation.expired",
                        schema_version=resolved.schema_version,
                        payload=observation_to_dict(resolved),
                    ),
                ),
            )
        )

    def read_outcome(self, outcome_id: OutcomeId) -> Outcome | None:
        projection = self.ledger.read_projection(
            aggregate_type=OUTCOME_AGGREGATE_TYPE, aggregate_id=str(outcome_id)
        )
        if projection is None:
            return None
        return outcome_from_dict(projection.state)

    def list_observations(
        self, *, state: ObservationState | None = None
    ) -> tuple[ObservationRequest, ...]:
        records = self.ledger.list_projections(aggregate_type=OBSERVATION_AGGREGATE_TYPE)
        observations = tuple(observation_from_dict(record.state) for record in records)
        if state is None:
            return observations
        return tuple(observation for observation in observations if observation.state is state)

    def list_outcomes(self) -> tuple[Outcome, ...]:
        records = self.ledger.list_projections(aggregate_type=OUTCOME_AGGREGATE_TYPE)
        return tuple(outcome_from_dict(record.state) for record in records)

    def _require_observation(self, observation_id: ObservationId) -> ObservationRequest:
        observation = self.read_observation(observation_id)
        if observation is None:
            raise InvalidInputError(
                "no observation is registered for this id",
                details={"observation_id": str(observation_id)},
            )
        return observation

    def _append_observation(
        self, observation: ObservationRequest, *, expected_version: int
    ) -> None:
        self.ledger.append(
            AppendCommand(
                correlation_id=f"observation-register:{observation.id}",
                events=(
                    EventWrite(
                        aggregate_type=OBSERVATION_AGGREGATE_TYPE,
                        aggregate_id=str(observation.id),
                        expected_version=expected_version,
                        event_type="observation.registered",
                        schema_version=observation.schema_version,
                        payload=observation_to_dict(observation),
                    ),
                ),
            )
        )

    def _append_capture(self, resolved: ObservationRequest, outcome: Outcome) -> None:
        self.ledger.append(
            AppendCommand(
                correlation_id=f"observation-capture:{resolved.id}",
                events=(
                    EventWrite(
                        aggregate_type=OBSERVATION_AGGREGATE_TYPE,
                        aggregate_id=str(resolved.id),
                        expected_version=1,
                        event_type="observation.captured",
                        schema_version=resolved.schema_version,
                        payload=observation_to_dict(resolved),
                    ),
                    EventWrite(
                        aggregate_type=OUTCOME_AGGREGATE_TYPE,
                        aggregate_id=str(outcome.id),
                        expected_version=0,
                        event_type="outcome.recorded",
                        schema_version=outcome.schema_version,
                        payload=outcome_to_dict(outcome),
                    ),
                ),
            )
        )


__all__ = [
    "COMPLETENESS_DERIVATION_VERSION",
    "DEFAULT_WINDOW",
    "DEFAULT_WINDOWS",
    "OBSERVATION_AGGREGATE_TYPE",
    "OUTCOME_AGGREGATE_TYPE",
    "CompletenessReport",
    "OutcomeCaptureService",
    "classify_pull_request_outcome",
    "compute_completeness",
    "observation_id_for",
]
