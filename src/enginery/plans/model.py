"""``Plan`` and ``PlanMilestone``: an immutable, validated milestone graph.

A ``Plan`` is the schema a development plan is normalized into before any
child run is created from it. Its milestone dependency graph is a
work-dependency DAG only — it says nothing about git branch ancestry, which
is modeled separately in ``enginery.stacks`` because a milestone can be
work-ready before its branch is rebased onto its parent's current head, and
resolving one condition never implies the other is resolved.

Validation mirrors ``enginery.domain.workflow.manifest``'s node-graph check:
unknown dependency references and dependency cycles are rejected before a
``Plan`` can be constructed at all, so "cycles and unresolved dependencies
fail before execution" holds structurally rather than by convention.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import PlanId, PlanMilestoneId

_PLAN_KEYS = frozenset(
    {"id", "source_provider", "external_reference", "source_snapshot_reference", "milestones"}
)
_MILESTONE_KEYS = frozenset(
    {
        "id",
        "title",
        "objective",
        "acceptance_criteria",
        "repository",
        "risk_class",
        "dependencies",
    }
)


def _require_non_blank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{field_name} must be a non-blank string")
    return value


def _require_str_tuple(
    raw: Mapping[str, object], key: str, *, required: bool = False
) -> tuple[str, ...]:
    value = raw.get(key, () if not required else None)
    if required and value is None:
        raise InvalidInputError(f"plan milestone is missing required key {key!r}")
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise InvalidInputError(f"{key!r} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class PlanMilestone:
    """One normalized unit of a development plan, before it becomes a ``WorkItem``."""

    id: PlanMilestoneId
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    repository: str
    risk_class: RiskClass
    dependencies: tuple[PlanMilestoneId, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_blank(self.title, field_name="title")
        _require_non_blank(self.objective, field_name="objective")
        _require_non_blank(self.repository, field_name="repository")
        if not self.acceptance_criteria:
            raise InvalidInputError(
                "a plan milestone requires at least one acceptance criterion",
                details={"milestone_id": str(self.id)},
            )
        if self.id in self.dependencies:
            raise InvalidInputError(
                "a plan milestone cannot depend on itself", details={"milestone_id": str(self.id)}
            )
        if len(set(self.dependencies)) != len(self.dependencies):
            raise InvalidInputError(
                "a plan milestone cannot declare the same dependency twice",
                details={"milestone_id": str(self.id)},
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> PlanMilestone:
        unknown_keys = set(raw) - _MILESTONE_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "plan milestone declares unknown keys; milestones cannot embed executable payloads",
                details={"unknown_keys": sorted(unknown_keys)},
            )
        milestone_id = PlanMilestoneId(_require_non_blank(raw.get("id"), field_name="id"))
        title = _require_non_blank(raw.get("title"), field_name="title")
        objective = _require_non_blank(raw.get("objective"), field_name="objective")
        repository = _require_non_blank(raw.get("repository"), field_name="repository")
        risk_class_raw = raw.get("risk_class")
        if not isinstance(risk_class_raw, str):
            raise InvalidInputError("plan milestone 'risk_class' must be a string")
        try:
            risk_class = RiskClass(risk_class_raw)
        except ValueError as error:
            raise InvalidInputError(
                f"plan milestone declares an unknown risk_class {risk_class_raw!r}",
                details={"milestone_id": str(milestone_id)},
            ) from error
        acceptance_criteria = _require_str_tuple(raw, "acceptance_criteria", required=True)
        dependencies = tuple(
            PlanMilestoneId(value) for value in _require_str_tuple(raw, "dependencies")
        )
        return cls(
            id=milestone_id,
            title=title,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            repository=repository,
            risk_class=risk_class,
            dependencies=dependencies,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "title": self.title,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "repository": self.repository,
            "risk_class": self.risk_class.value,
            "dependencies": [str(dependency) for dependency in self.dependencies],
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """An immutable, validated development plan: a milestone dependency DAG."""

    id: PlanId
    source_provider: str
    external_reference: str
    source_snapshot_reference: str
    milestones: tuple[PlanMilestone, ...]

    def __post_init__(self) -> None:
        _require_non_blank(self.source_provider, field_name="source_provider")
        _require_non_blank(self.external_reference, field_name="external_reference")
        _require_non_blank(self.source_snapshot_reference, field_name="source_snapshot_reference")
        if not self.milestones:
            raise InvalidInputError("a plan must declare at least one milestone")
        milestone_ids = [milestone.id for milestone in self.milestones]
        seen: set[PlanMilestoneId] = set()
        duplicates: set[PlanMilestoneId] = set()
        for milestone_id in milestone_ids:
            if milestone_id in seen:
                duplicates.add(milestone_id)
            seen.add(milestone_id)
        if duplicates:
            raise InvalidInputError(
                "plan declares duplicate milestone ids",
                details={"milestone_ids": sorted(str(value) for value in duplicates)},
            )
        _validate_milestone_graph(self)

    @property
    def content_digest(self) -> Digest:
        """A deterministic digest of the full plan content.

        Used to derive stable, deterministic child-run identity so fan-out
        is idempotent across a coordinator restart: the same plan content
        always derives the same child-run identity for the same milestone.
        """
        return Digest.of_json(self.to_mapping())

    def milestone(self, milestone_id: PlanMilestoneId) -> PlanMilestone:
        for candidate in self.milestones:
            if candidate.id == milestone_id:
                return candidate
        raise InvalidInputError(
            "plan does not declare this milestone", details={"milestone_id": str(milestone_id)}
        )

    def topological_order(self) -> tuple[PlanMilestoneId, ...]:
        """A deterministic dependency-respecting order over every milestone.

        Ties are broken by milestone id so the same plan always yields the
        same order, which downstream fan-out and stack-position derivation
        both depend on for reproducibility.
        """
        by_id = {milestone.id: milestone for milestone in self.milestones}
        in_degree = {milestone.id: len(milestone.dependencies) for milestone in self.milestones}
        forward: dict[PlanMilestoneId, list[PlanMilestoneId]] = {
            milestone.id: [] for milestone in self.milestones
        }
        for milestone in self.milestones:
            for dependency in milestone.dependencies:
                forward[dependency].append(milestone.id)
        ordered: list[PlanMilestoneId] = []
        remaining = dict(in_degree)
        frontier = sorted(
            (milestone_id for milestone_id, degree in remaining.items() if degree == 0),
            key=str,
        )
        while frontier:
            current = frontier.pop(0)
            ordered.append(current)
            successors = sorted(forward[current], key=str)
            for successor in successors:
                remaining[successor] -= 1
                if remaining[successor] == 0:
                    frontier.append(successor)
            frontier.sort(key=str)
        assert len(ordered) == len(by_id)  # validated acyclic at construction time
        return tuple(ordered)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Plan:
        unknown_keys = set(raw) - _PLAN_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "plan declares unknown keys; plans cannot embed executable payloads",
                details={"unknown_keys": sorted(unknown_keys)},
            )
        plan_id = PlanId(_require_non_blank(raw.get("id"), field_name="id"))
        source_provider = _require_non_blank(
            raw.get("source_provider"), field_name="source_provider"
        )
        external_reference = _require_non_blank(
            raw.get("external_reference"), field_name="external_reference"
        )
        source_snapshot_reference = _require_non_blank(
            raw.get("source_snapshot_reference"), field_name="source_snapshot_reference"
        )
        milestones_raw = raw.get("milestones")
        if not isinstance(milestones_raw, (list, tuple)) or not milestones_raw:
            raise InvalidInputError("plan 'milestones' must be a non-empty list")
        milestones: list[PlanMilestone] = []
        for entry in milestones_raw:
            if not isinstance(entry, Mapping):
                raise InvalidInputError("each plan milestone must be a mapping")
            milestones.append(PlanMilestone.from_mapping(entry))
        return cls(
            id=plan_id,
            source_provider=source_provider,
            external_reference=external_reference,
            source_snapshot_reference=source_snapshot_reference,
            milestones=tuple(milestones),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "source_provider": self.source_provider,
            "external_reference": self.external_reference,
            "source_snapshot_reference": self.source_snapshot_reference,
            "milestones": [milestone.to_mapping() for milestone in self.milestones],
        }


def _validate_milestone_graph(plan: Plan) -> None:
    known_ids = {milestone.id for milestone in plan.milestones}
    for milestone in plan.milestones:
        for dependency in milestone.dependencies:
            if dependency not in known_ids:
                raise InvalidInputError(
                    "plan milestone declares a dependency on an unresolved milestone",
                    details={
                        "milestone_id": str(milestone.id),
                        "unresolved_dependency": str(dependency),
                    },
                )

    forward: dict[PlanMilestoneId, list[PlanMilestoneId]] = {
        milestone.id: [] for milestone in plan.milestones
    }
    in_degree: dict[PlanMilestoneId, int] = {}
    for milestone in plan.milestones:
        in_degree[milestone.id] = len(milestone.dependencies)
        for dependency in milestone.dependencies:
            forward[dependency].append(milestone.id)

    entry_milestones = [milestone_id for milestone_id, degree in in_degree.items() if degree == 0]
    if not entry_milestones:
        raise InvalidInputError(
            "plan has no entry milestone (a milestone with no dependencies); "
            "every milestone participates in a cycle"
        )

    remaining = dict(in_degree)
    frontier = list(entry_milestones)
    visited: set[PlanMilestoneId] = set()
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        for successor in forward[current]:
            remaining[successor] -= 1
            if remaining[successor] == 0:
                frontier.append(successor)
    if len(visited) != len(plan.milestones):
        cyclic = sorted(
            str(milestone_id) for milestone_id in forward if milestone_id not in visited
        )
        raise InvalidInputError(
            "plan contains a milestone dependency cycle", details={"milestones": cyclic}
        )


__all__ = ["Plan", "PlanMilestone"]
