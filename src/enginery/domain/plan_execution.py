"""``PlanExecution``: one running fan-out/join instance of a validated ``Plan``.

A ``PlanExecution`` never stores milestone content, dependency structure,
or acceptance criteria — that is the immutable ``Plan``'s job
(``enginery.plans.model``). It stores exactly one thing per milestone: the
child run bound to that milestone, if any, and that child's lifecycle
state as observed by the process manager. This split mirrors ``Run`` vs
``WorkItem`` (a ``Run`` never re-declares a ``WorkItem``'s content either)
and keeps this module import-free of the ``plans`` package entirely, so
the domain layer's "domain imports only domain" boundary holds structurally.

A child failing or blocking never erases a completed sibling's recorded
state: each milestone's link is independent, and the plan-level ``state``
property is a pure roll-up over the current link states rather than a
separately mutated field, so it can never drift out of sync with what is
actually recorded per milestone.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PlanExecutionId, PlanId, PlanMilestoneId, RunId
from enginery.domain.immutable import freeze_mapping
from enginery.domain.state_machine import TransitionTable


class MilestoneRunState(enum.Enum):
    """The six states one milestone's child-run link may occupy."""

    PENDING = "pending"
    REGISTERED = "registered"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


MILESTONE_RUN_TRANSITIONS: TransitionTable[MilestoneRunState] = TransitionTable(
    edges={
        MilestoneRunState.PENDING: frozenset(
            {MilestoneRunState.REGISTERED, MilestoneRunState.CANCELLED}
        ),
        MilestoneRunState.REGISTERED: frozenset(
            {
                MilestoneRunState.SUCCEEDED,
                MilestoneRunState.BLOCKED,
                MilestoneRunState.FAILED,
                MilestoneRunState.CANCELLED,
            }
        ),
    },
    terminal_states=frozenset(
        {
            MilestoneRunState.SUCCEEDED,
            MilestoneRunState.BLOCKED,
            MilestoneRunState.FAILED,
            MilestoneRunState.CANCELLED,
        }
    ),
)

_BOUND_RUN_STATES = frozenset(
    {
        MilestoneRunState.REGISTERED,
        MilestoneRunState.SUCCEEDED,
        MilestoneRunState.BLOCKED,
        MilestoneRunState.FAILED,
    }
)


class PlanExecutionState(enum.Enum):
    """The plan-level roll-up over every milestone's current link state."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MilestoneRunLink:
    """One milestone's binding to (at most) one child run and its state."""

    milestone_id: PlanMilestoneId
    state: MilestoneRunState
    run_id: RunId | None = None

    def __post_init__(self) -> None:
        if self.state in _BOUND_RUN_STATES and self.run_id is None:
            raise InvalidInputError(
                "a milestone link past PENDING requires a bound run_id",
                details={"milestone_id": str(self.milestone_id), "state": self.state.value},
            )
        if self.state is MilestoneRunState.PENDING and self.run_id is not None:
            raise InvalidInputError(
                "a pending milestone link cannot already be bound to a run",
                details={"milestone_id": str(self.milestone_id)},
            )

    def transition_to(
        self, target: MilestoneRunState, *, run_id: RunId | None = None
    ) -> MilestoneRunLink:
        """Return a new link in ``target`` state, or raise if the transition
        is not legal from the current state."""
        MILESTONE_RUN_TRANSITIONS.require(self.state, target)
        return replace(self, state=target, run_id=run_id if run_id is not None else self.run_id)


@dataclass(frozen=True, slots=True)
class PlanExecution:
    """The durable fan-out/join process state for one running plan instance."""

    id: PlanExecutionId
    plan_id: PlanId
    plan_digest: Digest
    milestones: Mapping[PlanMilestoneId, MilestoneRunLink]
    aggregate_version: int = field(default=0)

    def __post_init__(self) -> None:
        if not self.milestones:
            raise InvalidInputError("a plan execution must track at least one milestone")
        for milestone_id, link in self.milestones.items():
            if link.milestone_id != milestone_id:
                raise InvalidInputError(
                    "a plan execution's milestone map key must match its link's milestone_id",
                    details={"key": str(milestone_id), "link_milestone_id": str(link.milestone_id)},
                )
        if self.aggregate_version < 0:
            raise InvalidInputError(
                "aggregate_version cannot be negative",
                details={"aggregate_version": self.aggregate_version},
            )
        freeze_mapping(self, "milestones", self.milestones)

    @property
    def state(self) -> PlanExecutionState:
        """The plan-level terminal or in-progress roll-up state.

        Precedence: any ``FAILED`` milestone makes the whole plan
        ``FAILED``; otherwise any ``BLOCKED`` milestone makes it
        ``BLOCKED``; otherwise, once every milestone has reached
        ``SUCCEEDED`` or ``CANCELLED``, the plan is ``CANCELLED`` if any
        milestone was cancelled, else ``SUCCEEDED``; otherwise the plan is
        still ``RUNNING``. A completed sibling's own ``SUCCEEDED`` link is
        never rewritten by another milestone's failure.
        """
        statuses = {link.state for link in self.milestones.values()}
        if MilestoneRunState.FAILED in statuses:
            return PlanExecutionState.FAILED
        if MilestoneRunState.BLOCKED in statuses:
            return PlanExecutionState.BLOCKED
        settled = {MilestoneRunState.SUCCEEDED, MilestoneRunState.CANCELLED}
        if statuses - settled:
            return PlanExecutionState.RUNNING
        if MilestoneRunState.CANCELLED in statuses:
            return PlanExecutionState.CANCELLED
        return PlanExecutionState.SUCCEEDED

    def link(self, milestone_id: PlanMilestoneId) -> MilestoneRunLink:
        link = self.milestones.get(milestone_id)
        if link is None:
            raise InvalidInputError(
                "plan execution does not track this milestone",
                details={"milestone_id": str(milestone_id)},
            )
        return link

    def run_id_for(self, milestone_id: PlanMilestoneId) -> RunId | None:
        return self.link(milestone_id).run_id

    def with_milestone(self, link: MilestoneRunLink) -> PlanExecution:
        """Return a new ``PlanExecution`` with one milestone's link replaced."""
        if link.milestone_id not in self.milestones:
            raise InvalidInputError(
                "plan execution does not track this milestone",
                details={"milestone_id": str(link.milestone_id)},
            )
        updated = dict(self.milestones)
        updated[link.milestone_id] = link
        return replace(self, milestones=updated, aggregate_version=self.aggregate_version + 1)

    @classmethod
    def initial(
        cls,
        *,
        plan_execution_id: PlanExecutionId,
        plan_id: PlanId,
        plan_digest: Digest,
        milestone_ids: Iterable[PlanMilestoneId],
    ) -> PlanExecution:
        """Seed every declared milestone as ``PENDING`` with no bound run."""
        milestones = {
            milestone_id: MilestoneRunLink(
                milestone_id=milestone_id, state=MilestoneRunState.PENDING
            )
            for milestone_id in milestone_ids
        }
        return cls(
            id=plan_execution_id, plan_id=plan_id, plan_digest=plan_digest, milestones=milestones
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "plan_id": str(self.plan_id),
            "plan_digest": str(self.plan_digest),
            "milestones": {
                str(milestone_id): {
                    "milestone_id": str(link.milestone_id),
                    "state": link.state.value,
                    "run_id": str(link.run_id) if link.run_id is not None else None,
                }
                for milestone_id, link in self.milestones.items()
            },
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> PlanExecution:
        plan_execution_id = PlanExecutionId(_str(raw, "id"))
        plan_id = PlanId(_str(raw, "plan_id"))
        plan_digest = _digest(_str(raw, "plan_digest"))
        milestones_raw = raw.get("milestones")
        if not isinstance(milestones_raw, Mapping):
            raise InvalidInputError("plan execution 'milestones' must be a mapping")
        milestones: dict[PlanMilestoneId, MilestoneRunLink] = {}
        for key, entry in milestones_raw.items():
            if not isinstance(entry, Mapping):
                raise InvalidInputError("each plan execution milestone entry must be a mapping")
            run_id_raw = entry.get("run_id")
            milestones[PlanMilestoneId(str(key))] = MilestoneRunLink(
                milestone_id=PlanMilestoneId(_str(entry, "milestone_id")),
                state=MilestoneRunState(_str(entry, "state")),
                run_id=RunId(run_id_raw) if isinstance(run_id_raw, str) else None,
            )
        aggregate_version = raw.get("aggregate_version")
        if not isinstance(aggregate_version, int) or isinstance(aggregate_version, bool):
            raise InvalidInputError("plan execution 'aggregate_version' must be an integer")
        return cls(
            id=plan_execution_id,
            plan_id=plan_id,
            plan_digest=plan_digest,
            milestones=milestones,
            aggregate_version=aggregate_version,
        )


def _str(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"plan execution is missing required non-blank key {key!r}")
    return value


def _digest(value: str) -> Digest:
    if ":" not in value:
        raise InvalidInputError("plan execution 'plan_digest' must be an 'algorithm:hex' digest")
    algorithm, _, hex_value = value.partition(":")
    return Digest(algorithm=algorithm, hex_value=hex_value)


__all__ = [
    "MILESTONE_RUN_TRANSITIONS",
    "MilestoneRunLink",
    "MilestoneRunState",
    "PlanExecution",
    "PlanExecutionState",
]
