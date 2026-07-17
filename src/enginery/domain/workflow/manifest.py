"""``WorkflowManifest``: an immutable, versioned, validated workflow graph
(03_SYSTEM_DESIGN.md §9.2, §12.1).

Validation rejects unknown node references, dependency cycles, an absent
entry node, and terminal-state claims that no path can actually reach
("unreachable terminal claims"). ``operation_id_for`` derives a stable
operation identity for a side-effecting node using only run, node,
side-effect kind (the node's registered ``NodeKind``), target scope, and
ordinal — never an attempt number (§7.10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import NodeId, OperationId, RunId, WorkflowDefinitionId
from enginery.domain.immutable import freeze_mapping
from enginery.domain.workflow.node import BranchCondition, NodeDeclaration, SideEffectClass
from enginery.domain.workflow.schema import FieldSchema, IOSchema

_MANIFEST_KEYS = frozenset(
    {
        "id",
        "name",
        "schema_version",
        "nodes",
        "terminal_states",
        "terminal_state_mapping",
        "input_schema",
        "output_schema",
        "compatibility",
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowManifest:
    """An immutable, versioned directed graph of typed node declarations."""

    id: WorkflowDefinitionId
    name: str
    schema_version: int
    nodes: Mapping[NodeId, NodeDeclaration]
    terminal_states: frozenset[str]
    terminal_state_mapping: Mapping[NodeId, str]
    input_schema: IOSchema = field(default_factory=IOSchema)
    output_schema: IOSchema = field(default_factory=IOSchema)
    compatibility: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_manifest(self)
        freeze_mapping(self, "nodes", self.nodes)
        freeze_mapping(self, "terminal_state_mapping", self.terminal_state_mapping)
        freeze_mapping(self, "compatibility", self.compatibility)

    @property
    def content_digest(self) -> Digest:
        """A deterministic digest of the full manifest content (§9.2)."""
        return Digest.of_json(_manifest_to_json(self))

    def to_mapping(self) -> dict[str, object]:
        """A JSON-serializable mapping accepted verbatim by ``from_mapping``."""
        return _manifest_to_json(self)

    def operation_id_for(
        self, *, run_id: RunId, node_id: NodeId, target_scope: str, ordinal: int
    ) -> OperationId:
        """Derive the stable operation ID for one side-effecting node (§7.10).

        The logical side-effect kind is the node's registered ``NodeKind``;
        callers never supply it separately, so the same node always derives
        the same identity for the same run, target scope, and ordinal.
        """
        node = self.nodes.get(node_id)
        if node is None:
            raise InvalidInputError(
                "cannot derive an operation id for an unknown node",
                details={"node_id": str(node_id)},
            )
        if node.side_effect_class is SideEffectClass.NONE:
            raise InvalidInputError(
                "cannot derive an operation id for a non-side-effecting node",
                details={"node_id": str(node_id)},
            )
        return OperationId.derive(
            run_id=run_id,
            node_id=node_id,
            side_effect_kind=node.kind.value,
            target_scope=target_scope,
            ordinal=ordinal,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> WorkflowManifest:
        unknown_keys = set(raw) - _MANIFEST_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "workflow manifest declares unknown keys; manifests cannot embed "
                "executable payloads",
                details={"unknown_keys": sorted(unknown_keys)},
            )
        manifest_id = WorkflowDefinitionId(_require_str(raw, "id"))
        name = _require_str(raw, "name")
        schema_version = raw.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise InvalidInputError("workflow manifest 'schema_version' must be an integer")
        nodes_raw = raw.get("nodes")
        if not isinstance(nodes_raw, Mapping):
            raise InvalidInputError("workflow manifest 'nodes' must be a mapping")
        nodes: dict[NodeId, NodeDeclaration] = {}
        for key, node_raw in nodes_raw.items():
            if not isinstance(node_raw, Mapping):
                raise InvalidInputError(
                    "each node declaration must be a mapping", details={"node_id": str(key)}
                )
            node_id = NodeId(key)
            nodes[node_id] = NodeDeclaration.from_mapping(node_id, node_raw)
        terminal_states = frozenset(_optional_str_tuple(raw, "terminal_states"))
        terminal_state_mapping_raw = raw.get("terminal_state_mapping", {})
        if not isinstance(terminal_state_mapping_raw, Mapping):
            raise InvalidInputError("workflow manifest 'terminal_state_mapping' must be a mapping")
        terminal_state_mapping: dict[NodeId, str] = {}
        for key, value in terminal_state_mapping_raw.items():
            if not isinstance(value, str):
                raise InvalidInputError(
                    "terminal_state_mapping values must be strings", details={"node_id": str(key)}
                )
            terminal_state_mapping[NodeId(key)] = value
        input_schema_raw = raw.get("input_schema", {})
        output_schema_raw = raw.get("output_schema", {})
        if not isinstance(input_schema_raw, Mapping) or not isinstance(output_schema_raw, Mapping):
            raise InvalidInputError("workflow manifest schemas must be mappings")
        compatibility_raw = raw.get("compatibility", {})
        if not isinstance(compatibility_raw, Mapping):
            raise InvalidInputError("workflow manifest 'compatibility' must be a mapping")
        compatibility: dict[str, str] = {}
        for key, value in compatibility_raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise InvalidInputError("compatibility keys and values must be strings")
            compatibility[key] = value
        return cls(
            id=manifest_id,
            name=name,
            schema_version=schema_version,
            nodes=nodes,
            terminal_states=terminal_states,
            terminal_state_mapping=terminal_state_mapping,
            input_schema=IOSchema.from_mapping(input_schema_raw),
            output_schema=IOSchema.from_mapping(output_schema_raw),
            compatibility=compatibility,
        )


def _require_str(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"workflow manifest is missing required non-blank key {key!r}")
    return value


def _optional_str_tuple(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key, ())
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise InvalidInputError(f"{key!r} must be a list of strings")
    return tuple(value)


def _validate_manifest(manifest: WorkflowManifest) -> None:
    if not manifest.name.strip():
        raise InvalidInputError("workflow manifest name must be a non-blank string")
    if manifest.schema_version < 1:
        raise InvalidInputError(
            "schema_version must be at least 1",
            details={"schema_version": manifest.schema_version},
        )
    if not manifest.nodes:
        raise InvalidInputError("workflow manifest must declare at least one node")
    if not manifest.terminal_states:
        raise InvalidInputError("workflow manifest must declare at least one terminal state")

    for node_id, node in manifest.nodes.items():
        for dependency in node.dependencies:
            if dependency not in manifest.nodes:
                raise InvalidInputError(
                    "node declares a dependency on an unknown node",
                    details={"node_id": str(node_id), "unknown_dependency": str(dependency)},
                )

    forward: dict[NodeId, list[NodeId]] = {node_id: [] for node_id in manifest.nodes}
    in_degree: dict[NodeId, int] = {}
    for node_id, node in manifest.nodes.items():
        in_degree[node_id] = len(node.dependencies)
        for dependency in node.dependencies:
            forward[dependency].append(node_id)

    entry_nodes = [node_id for node_id, degree in in_degree.items() if degree == 0]
    if not entry_nodes:
        raise InvalidInputError(
            "workflow manifest has no entry node (a node with no dependencies); "
            "every node participates in a cycle"
        )

    remaining = dict(in_degree)
    frontier = list(entry_nodes)
    visited: set[NodeId] = set()
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        for successor in forward[current]:
            remaining[successor] -= 1
            if remaining[successor] == 0:
                frontier.append(successor)
    if len(visited) != len(manifest.nodes):
        cyclic = sorted(str(node_id) for node_id in manifest.nodes if node_id not in visited)
        raise InvalidInputError(
            "workflow manifest contains a dependency cycle", details={"nodes": cyclic}
        )

    for node_id, state_name in manifest.terminal_state_mapping.items():
        if node_id not in manifest.nodes:
            raise InvalidInputError(
                "terminal_state_mapping references an unknown node",
                details={"node_id": str(node_id)},
            )
        if state_name not in manifest.terminal_states:
            raise InvalidInputError(
                "terminal_state_mapping references an undeclared terminal state",
                details={"node_id": str(node_id), "state": state_name},
            )
        if node_id not in visited:
            raise InvalidInputError(
                "terminal_state_mapping claims a node unreachable from any entry node",
                details={"node_id": str(node_id)},
            )
        if forward[node_id]:
            raise InvalidInputError(
                "terminal_state_mapping claims a node with downstream dependents; "
                "a workflow terminal claim is unreachable as an actual endpoint "
                "when execution necessarily continues past it",
                details={
                    "node_id": str(node_id),
                    "dependents": sorted(str(dependent) for dependent in forward[node_id]),
                },
            )


def _branch_condition_to_json(condition: BranchCondition) -> dict[str, object]:
    return {
        "operator": condition.operator.value,
        "field": condition.field_path,
        "values": list(condition.values),
    }


def _field_schema_to_json(item: FieldSchema) -> dict[str, object]:
    return {"type": item.field_type.value, "required": item.required}


def _schema_to_json(schema: IOSchema) -> dict[str, dict[str, object]]:
    return {item.name: _field_schema_to_json(item) for item in schema.fields}


def _node_to_json(node: NodeDeclaration) -> dict[str, object]:
    return {
        "kind": node.kind.value,
        "input_schema": _schema_to_json(node.input_schema),
        "output_schema": _schema_to_json(node.output_schema),
        "actor_type": node.actor_type.value,
        "side_effect_class": node.side_effect_class.value,
        "idempotency_behavior": node.idempotency_behavior.value,
        "reconciliation_operation": node.reconciliation_operation,
        "evidence_contract": sorted(node.evidence_contract),
        "emitted_event_types": sorted(node.emitted_event_types),
        "policy_action": node.policy_action,
        "required_capabilities": sorted(node.required_capabilities),
        "dependencies": sorted(str(dependency) for dependency in node.dependencies),
        "branch_conditions": [
            _branch_condition_to_json(condition) for condition in node.branch_conditions
        ],
        "parallel_group": node.parallel_group,
        "subworkflow": str(node.subworkflow) if node.subworkflow is not None else None,
        "budget": {
            "max_attempts": node.budget.max_attempts,
            "max_duration_seconds": node.budget.max_duration_seconds,
            "max_cost": node.budget.max_cost,
        },
    }


def _manifest_to_json(manifest: WorkflowManifest) -> dict[str, object]:
    return {
        "id": str(manifest.id),
        "name": manifest.name,
        "schema_version": manifest.schema_version,
        "nodes": {str(node_id): _node_to_json(node) for node_id, node in manifest.nodes.items()},
        "terminal_states": sorted(manifest.terminal_states),
        "terminal_state_mapping": {
            str(node_id): state for node_id, state in manifest.terminal_state_mapping.items()
        },
        "input_schema": _schema_to_json(manifest.input_schema),
        "output_schema": _schema_to_json(manifest.output_schema),
        "compatibility": dict(sorted(manifest.compatibility.items())),
    }


__all__ = ["WorkflowManifest"]
