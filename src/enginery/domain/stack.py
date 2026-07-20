"""``Stack`` and ``StackSlice``: branch-topology evidence for a plan's milestones.

A milestone's work being ready (every dependency succeeded) and its
branch being ready to merge (fresh CI at the current head, based on an
already-merged parent) are independent conditions, tracked by two
separate structures: ``enginery.domain.plan_execution.PlanExecution``
owns the work-dependency axis, this module owns the git-ancestry axis. A
milestone can be work-ready while its branch still needs a rebase after
an earlier slice merged; a branch can carry fresh green CI while its
milestone's own acceptance criteria are still unmet. Conflating the two
-- inferring merge readiness from work-dependency state or vice versa --
is exactly the risk this split keeps out.

A ``StackSlice`` never carries a milestone's objective, acceptance
criteria, or dependency content -- only ``PlanMilestoneId`` links back to
the plan that owns that content. Nothing in this module performs a merge
or a release; it only models and projects readiness.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PlanId, PlanMilestoneId, StackId
from enginery.domain.immutable import freeze_mapping
from enginery.domain.state_machine import TransitionTable


class StackSliceState(enum.Enum):
    """The six states one stack slice's branch may occupy."""

    PENDING = "pending"
    PUBLISHED = "published"
    MERGE_READY = "merge_ready"
    STALE = "stale"
    MERGED = "merged"
    ABANDONED = "abandoned"


STACK_SLICE_TRANSITIONS: TransitionTable[StackSliceState] = TransitionTable(
    edges={
        StackSliceState.PENDING: frozenset({StackSliceState.PUBLISHED, StackSliceState.ABANDONED}),
        StackSliceState.PUBLISHED: frozenset(
            {
                StackSliceState.PUBLISHED,
                StackSliceState.MERGE_READY,
                StackSliceState.STALE,
                StackSliceState.ABANDONED,
            }
        ),
        StackSliceState.MERGE_READY: frozenset(
            {
                StackSliceState.PUBLISHED,
                StackSliceState.STALE,
                StackSliceState.MERGED,
                StackSliceState.ABANDONED,
            }
        ),
        StackSliceState.STALE: frozenset({StackSliceState.PUBLISHED, StackSliceState.ABANDONED}),
    },
    terminal_states=frozenset({StackSliceState.MERGED, StackSliceState.ABANDONED}),
)


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{field_name} must be a non-blank string")


@dataclass(frozen=True, slots=True)
class StackSlice:
    """One milestone's position, branch, and merge-readiness evidence."""

    milestone_id: PlanMilestoneId
    position: int
    base_ref: str
    branch_ref: str
    state: StackSliceState = StackSliceState.PENDING
    head_revision: str | None = None
    ci_evidence_digest: Digest | None = None

    def __post_init__(self) -> None:
        if self.position < 1:
            raise InvalidInputError(
                "a stack slice position must be at least 1 (root)",
                details={"milestone_id": str(self.milestone_id), "position": self.position},
            )
        _require_non_blank(self.base_ref, field_name="base_ref")
        _require_non_blank(self.branch_ref, field_name="branch_ref")
        if self.state is not StackSliceState.PENDING and self.head_revision is None:
            raise InvalidInputError(
                "a published or later stack slice requires a bound head_revision",
                details={"milestone_id": str(self.milestone_id), "state": self.state.value},
            )
        if self.state is StackSliceState.PENDING and self.head_revision is not None:
            raise InvalidInputError(
                "a pending stack slice cannot already carry a head_revision",
                details={"milestone_id": str(self.milestone_id)},
            )
        if self.state is StackSliceState.MERGE_READY and self.ci_evidence_digest is None:
            raise InvalidInputError(
                "a merge-ready stack slice requires bound CI evidence",
                details={"milestone_id": str(self.milestone_id)},
            )

    def publish(self, *, head_revision: str) -> StackSlice:
        """Record a pushed branch head.

        Legal from ``PENDING``, ``PUBLISHED``, ``STALE``, or
        ``MERGE_READY`` -- a fresh push at any point before a merge
        invalidates whatever CI evidence existed for the prior head
        exactly as a rebase does, so it (re-)enters ``PUBLISHED`` and
        needs fresh evidence before it can be ``MERGE_READY`` again.
        """
        STACK_SLICE_TRANSITIONS.require(self.state, StackSliceState.PUBLISHED)
        _require_non_blank(head_revision, field_name="head_revision")
        return replace(
            self,
            state=StackSliceState.PUBLISHED,
            head_revision=head_revision,
            ci_evidence_digest=None,
        )

    def mark_merge_ready(self, *, head_revision: str, ci_evidence_digest: Digest) -> StackSlice:
        """Record fresh CI evidence bound to the slice's current head exactly."""
        STACK_SLICE_TRANSITIONS.require(self.state, StackSliceState.MERGE_READY)
        if self.head_revision != head_revision:
            raise InvalidInputError(
                "CI evidence does not match this slice's current published head",
                details={
                    "milestone_id": str(self.milestone_id),
                    "expected_head_revision": self.head_revision,
                    "observed_head_revision": head_revision,
                },
            )
        return replace(
            self, state=StackSliceState.MERGE_READY, ci_evidence_digest=ci_evidence_digest
        )

    def mark_stale(self) -> StackSlice:
        """The base moved beneath this slice; prior CI evidence no longer applies.

        A no-op for a slice that never published a branch -- there is
        nothing beneath a ``PENDING`` slice to invalidate.
        """
        if self.state is StackSliceState.PENDING:
            return self
        STACK_SLICE_TRANSITIONS.require(self.state, StackSliceState.STALE)
        return replace(self, state=StackSliceState.STALE, ci_evidence_digest=None)

    def mark_merged(self) -> StackSlice:
        STACK_SLICE_TRANSITIONS.require(self.state, StackSliceState.MERGED)
        return replace(self, state=StackSliceState.MERGED)

    def abandon(self) -> StackSlice:
        STACK_SLICE_TRANSITIONS.require(self.state, StackSliceState.ABANDONED)
        return replace(self, state=StackSliceState.ABANDONED)

    def to_mapping(self) -> dict[str, object]:
        return {
            "milestone_id": str(self.milestone_id),
            "position": self.position,
            "base_ref": self.base_ref,
            "branch_ref": self.branch_ref,
            "state": self.state.value,
            "head_revision": self.head_revision,
            "ci_evidence_digest": (
                str(self.ci_evidence_digest) if self.ci_evidence_digest is not None else None
            ),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> StackSlice:
        position = raw.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            raise InvalidInputError("stack slice 'position' must be an integer")
        head_revision = raw.get("head_revision")
        ci_evidence_raw = raw.get("ci_evidence_digest")
        return cls(
            milestone_id=PlanMilestoneId(_str(raw, "milestone_id")),
            position=position,
            base_ref=_str(raw, "base_ref"),
            branch_ref=_str(raw, "branch_ref"),
            state=StackSliceState(_str(raw, "state")),
            head_revision=head_revision if isinstance(head_revision, str) else None,
            ci_evidence_digest=(
                _digest(ci_evidence_raw) if isinstance(ci_evidence_raw, str) else None
            ),
        )


@dataclass(frozen=True, slots=True)
class Stack:
    """The durable branch-topology record for one plan: an ordered slice chain."""

    id: StackId
    plan_id: PlanId
    base_ref: str
    slices: Mapping[PlanMilestoneId, StackSlice]
    aggregate_version: int = field(default=0)

    def __post_init__(self) -> None:
        if not self.slices:
            raise InvalidInputError("a stack must track at least one slice")
        _require_non_blank(self.base_ref, field_name="base_ref")
        for milestone_id, slice_ in self.slices.items():
            if slice_.milestone_id != milestone_id:
                raise InvalidInputError(
                    "a stack's slice map key must match its slice's milestone_id",
                    details={
                        "key": str(milestone_id),
                        "slice_milestone_id": str(slice_.milestone_id),
                    },
                )
        positions = sorted(slice_.position for slice_ in self.slices.values())
        if positions != list(range(1, len(positions) + 1)):
            raise InvalidInputError(
                "a stack's slice positions must be exactly 1..N with no gaps or duplicates",
                details={"positions": positions},
            )
        if self.aggregate_version < 0:
            raise InvalidInputError(
                "aggregate_version cannot be negative",
                details={"aggregate_version": self.aggregate_version},
            )
        freeze_mapping(self, "slices", self.slices)

    @property
    def ordered_slices(self) -> tuple[StackSlice, ...]:
        return tuple(sorted(self.slices.values(), key=lambda item: item.position))

    def slice(self, milestone_id: PlanMilestoneId) -> StackSlice:
        found = self.slices.get(milestone_id)
        if found is None:
            raise InvalidInputError(
                "stack does not track this milestone",
                details={"milestone_id": str(milestone_id)},
            )
        return found

    def with_slice(self, updated: StackSlice) -> Stack:
        if updated.milestone_id not in self.slices:
            raise InvalidInputError(
                "stack does not track this milestone",
                details={"milestone_id": str(updated.milestone_id)},
            )
        new_slices = dict(self.slices)
        new_slices[updated.milestone_id] = updated
        return replace(self, slices=new_slices, aggregate_version=self.aggregate_version + 1)

    def reconcile_after_publish(
        self, milestone_id: PlanMilestoneId, *, head_revision: str
    ) -> Stack:
        """Publish one slice's new head, then mark every later slice ``STALE``.

        A branch's ancestry above a rebased/republished slice is no
        longer trustworthy until it is itself rebased and republished:
        this is what "branch ancestry ... must survive restart/rebase/
        reconciliation" requires -- a lower slice changing beneath a
        higher one is detected and recorded, never silently ignored.
        """
        target = self.slice(milestone_id)
        current = self.with_slice(target.publish(head_revision=head_revision))
        for later in current.ordered_slices:
            if later.position > target.position:
                current = current.with_slice(later.mark_stale())
        return current

    def next_mergeable(self) -> PlanMilestoneId | None:
        """The single next slice eligible for a root-to-leaf merge, if any.

        ``None`` when the next unmerged position is not yet
        ``MERGE_READY`` (still ``PENDING``, ``PUBLISHED``, or ``STALE``),
        or when every slice is already ``MERGED``.
        """
        for slice_ in self.ordered_slices:
            if slice_.state is StackSliceState.MERGED:
                continue
            if slice_.state is StackSliceState.MERGE_READY:
                return slice_.milestone_id
            return None
        return None

    def mark_merged(self, milestone_id: PlanMilestoneId) -> Stack:
        """Record ``milestone_id``'s slice as merged.

        Requires every earlier position to already be merged, enforcing
        root-to-leaf order structurally rather than by caller discipline.
        """
        target = self.slice(milestone_id)
        for earlier in self.ordered_slices:
            if earlier.position < target.position and earlier.state is not StackSliceState.MERGED:
                raise InvalidInputError(
                    "cannot merge a slice before an earlier slice in the stack has merged",
                    details={
                        "milestone_id": str(milestone_id),
                        "position": target.position,
                        "blocking_milestone_id": str(earlier.milestone_id),
                        "blocking_position": earlier.position,
                    },
                )
        return self.with_slice(target.mark_merged())

    @classmethod
    def initial(
        cls,
        *,
        stack_id: StackId,
        plan_id: PlanId,
        base_ref: str,
        ordered_milestones: Iterable[tuple[PlanMilestoneId, str]],
    ) -> Stack:
        """Seed a stack from a root-to-leaf ordered ``(milestone_id, branch_ref)`` sequence.

        Each slice's own ``base_ref`` is the prior slice's ``branch_ref``,
        or the stack's overall ``base_ref`` for the root (position 1).
        """
        slices: dict[PlanMilestoneId, StackSlice] = {}
        previous_branch = base_ref
        for position, (milestone_id, branch_ref) in enumerate(ordered_milestones, start=1):
            slices[milestone_id] = StackSlice(
                milestone_id=milestone_id,
                position=position,
                base_ref=previous_branch,
                branch_ref=branch_ref,
            )
            previous_branch = branch_ref
        return cls(id=stack_id, plan_id=plan_id, base_ref=base_ref, slices=slices)

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "plan_id": str(self.plan_id),
            "base_ref": self.base_ref,
            "slices": {
                str(milestone_id): slice_.to_mapping()
                for milestone_id, slice_ in self.slices.items()
            },
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Stack:
        slices_raw = raw.get("slices")
        if not isinstance(slices_raw, Mapping):
            raise InvalidInputError("stack 'slices' must be a mapping")
        slices: dict[PlanMilestoneId, StackSlice] = {}
        for key, entry in slices_raw.items():
            if not isinstance(entry, Mapping):
                raise InvalidInputError("each stack slice entry must be a mapping")
            slices[PlanMilestoneId(str(key))] = StackSlice.from_mapping(entry)
        aggregate_version = raw.get("aggregate_version")
        if not isinstance(aggregate_version, int) or isinstance(aggregate_version, bool):
            raise InvalidInputError("stack 'aggregate_version' must be an integer")
        return cls(
            id=StackId(_str(raw, "id")),
            plan_id=PlanId(_str(raw, "plan_id")),
            base_ref=_str(raw, "base_ref"),
            slices=slices,
            aggregate_version=aggregate_version,
        )


def _str(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"stack is missing required non-blank key {key!r}")
    return value


def _digest(value: str) -> Digest:
    if ":" not in value:
        raise InvalidInputError("stack 'ci_evidence_digest' must be an 'algorithm:hex' digest")
    algorithm, _, hex_value = value.partition(":")
    return Digest(algorithm=algorithm, hex_value=hex_value)


__all__ = [
    "STACK_SLICE_TRANSITIONS",
    "Stack",
    "StackSlice",
    "StackSliceState",
]
