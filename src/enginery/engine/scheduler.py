"""Deterministic readiness and bounded fair scheduling for workflow nodes."""

from __future__ import annotations

import enum
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError


class SchedulableState(enum.Enum):
    """Node states relevant to deterministic dispatch decisions."""

    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True, order=True)
class NodeKey:
    run_id: str
    node_id: str

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.node_id.strip():
            raise InvalidInputError("scheduled node keys require non-blank run_id and node_id")


@dataclass(frozen=True, slots=True)
class SchedulableNode:
    """The scheduler's provider-neutral view of one workflow node."""

    key: NodeKey
    dependencies: tuple[NodeKey, ...] = field(default_factory=tuple)
    state: SchedulableState = SchedulableState.QUEUED
    repository_id: str | None = None
    requirements_satisfied: bool = True
    queued_since: datetime | None = None

    def __post_init__(self) -> None:
        if self.key in self.dependencies:
            raise InvalidInputError(
                "a scheduled node cannot depend on itself", details={"node": self.key}
            )
        if self.repository_id is not None and not self.repository_id.strip():
            raise InvalidInputError("repository_id must be non-blank when supplied")


@dataclass(frozen=True, slots=True)
class SchedulingLimits:
    global_concurrency: int
    per_repository_concurrency: int

    def __post_init__(self) -> None:
        if self.global_concurrency < 1:
            raise InvalidInputError("global_concurrency must be positive")
        if self.per_repository_concurrency < 1:
            raise InvalidInputError("per_repository_concurrency must be positive")


class DeferralReason(enum.StrEnum):
    """Why a queued node was evaluated but not selected for dispatch."""

    GLOBAL_CAPACITY = "global_capacity"
    REPOSITORY_CAPACITY = "repository_capacity"
    DEPENDENCIES = "dependencies"
    DEPENDENCY_FAILED = "dependency_failed"
    REQUIREMENTS = "requirements"
    CAPACITY_RESERVED = "capacity_reserved_for_other_run"
    EXECUTION_HOLD = "execution_hold"


@dataclass(frozen=True, slots=True)
class DeferredNode:
    """One evaluated, still-queued node and the single reason it was not selected."""

    key: NodeKey
    reason: DeferralReason


@dataclass(frozen=True, slots=True)
class SchedulingPlan:
    selected: tuple[NodeKey, ...]
    deferred: tuple[DeferredNode, ...] = ()


_DEAD_DEPENDENCY_STATES = frozenset(
    {SchedulableState.FAILED, SchedulableState.CANCELLED, SchedulableState.BLOCKED}
)


class ReadinessScheduler:
    """Select ready nodes without mutating node or lease state."""

    def plan(
        self,
        nodes: Iterable[SchedulableNode],
        *,
        limits: SchedulingLimits,
    ) -> SchedulingPlan:
        """Select ready nodes across every run, oldest queued run first, within the limits."""
        by_key = _index_nodes(nodes)
        active = tuple(
            node
            for node in by_key.values()
            if node.state in {SchedulableState.LEASED, SchedulableState.RUNNING}
        )
        available_slots = limits.global_concurrency - len(active)
        repository_load = _repository_load(active)
        ready_by_run: dict[str, deque[SchedulableNode]] = defaultdict(deque)
        for node in sorted(by_key.values(), key=lambda item: item.key):
            if available_slots > 0 and _is_ready(node, by_key):
                ready_by_run[node.key.run_id].append(node)
        run_order = _oldest_first_run_order(ready_by_run)
        selected: list[NodeKey] = []
        while available_slots > 0 and run_order:
            progressed = False
            for run_id in tuple(run_order):
                queue = ready_by_run[run_id]
                candidate = _next_allowed_candidate(
                    queue,
                    repository_load=repository_load,
                    per_repository_limit=limits.per_repository_concurrency,
                )
                if candidate is None:
                    run_order.remove(run_id)
                    continue
                selected.append(candidate.key)
                available_slots -= 1
                progressed = True
                if candidate.repository_id is not None:
                    repository_load[candidate.repository_id] += 1
                if not queue:
                    run_order.remove(run_id)
                if available_slots == 0:
                    break
            if not progressed:
                break
        chosen = frozenset(selected)
        deferred = tuple(
            DeferredNode(
                node.key,
                _deferral_reason(node, by_key, repository_load, limits=limits),
            )
            for node in sorted(by_key.values(), key=lambda item: item.key)
            if node.state is SchedulableState.QUEUED and node.key not in chosen
        )
        return SchedulingPlan(selected=tuple(selected), deferred=deferred)


def _deferral_reason(
    node: SchedulableNode,
    nodes: dict[NodeKey, SchedulableNode],
    repository_load: dict[str, int],
    *,
    limits: SchedulingLimits,
) -> DeferralReason:
    if not node.requirements_satisfied:
        return DeferralReason.REQUIREMENTS
    dependency_states = [nodes[dependency].state for dependency in node.dependencies]
    if any(state in _DEAD_DEPENDENCY_STATES for state in dependency_states):
        return DeferralReason.DEPENDENCY_FAILED
    if any(state is not SchedulableState.SUCCEEDED for state in dependency_states):
        return DeferralReason.DEPENDENCIES
    if (
        node.repository_id is not None
        and repository_load.get(node.repository_id, 0) >= limits.per_repository_concurrency
    ):
        return DeferralReason.REPOSITORY_CAPACITY
    return DeferralReason.GLOBAL_CAPACITY


def _index_nodes(nodes: Iterable[SchedulableNode]) -> dict[NodeKey, SchedulableNode]:
    indexed: dict[NodeKey, SchedulableNode] = {}
    for node in nodes:
        if node.key in indexed:
            raise InvalidInputError(
                "scheduler input contains duplicate node keys", details={"node": node.key}
            )
        indexed[node.key] = node
    for node in indexed.values():
        unknown = [dependency for dependency in node.dependencies if dependency not in indexed]
        if unknown:
            raise InvalidInputError(
                "scheduler input contains unknown dependencies",
                details={"node": node.key, "dependencies": unknown},
            )
    return indexed


def _is_ready(node: SchedulableNode, nodes: dict[NodeKey, SchedulableNode]) -> bool:
    return (
        node.state is SchedulableState.QUEUED
        and node.requirements_satisfied
        and all(
            nodes[dependency].state is SchedulableState.SUCCEEDED
            for dependency in node.dependencies
        )
    )


def _repository_load(active: tuple[SchedulableNode, ...]) -> dict[str, int]:
    load: dict[str, int] = defaultdict(int)
    for node in active:
        if node.repository_id is not None:
            load[node.repository_id] += 1
    return load


def _oldest_first_run_order(ready_by_run: dict[str, deque[SchedulableNode]]) -> deque[str]:
    """Order runs by how long their oldest ready node has been queued, then by run id.

    The order depends only on persisted queue times, so every coordinator process derives
    the same one: a later-arriving run never overtakes an older queued run, whatever its id.
    A node with no recorded queue time (a record from before queue times were kept) ranks
    after every timed node.
    """

    def rank(run_id: str) -> tuple[bool, datetime, str]:
        times = [
            node.queued_since for node in ready_by_run[run_id] if node.queued_since is not None
        ]
        return (not times, min(times) if times else datetime.max.replace(tzinfo=UTC), run_id)

    return deque(sorted(ready_by_run, key=rank))


def _next_allowed_candidate(
    queue: deque[SchedulableNode],
    *,
    repository_load: dict[str, int],
    per_repository_limit: int,
) -> SchedulableNode | None:
    for candidate in tuple(queue):
        if (
            candidate.repository_id is None
            or repository_load[candidate.repository_id] < per_repository_limit
        ):
            queue.remove(candidate)
            return candidate
    return None


__all__ = [
    "DeferralReason",
    "DeferredNode",
    "NodeKey",
    "ReadinessScheduler",
    "SchedulableNode",
    "SchedulableState",
    "SchedulingLimits",
    "SchedulingPlan",
]
