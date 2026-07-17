"""Typed node declarations for workflow manifests.

Executable behavior lives in typed, tested modules outside this package;
``NodeDeclaration`` only carries the *contract* a registered node type
fulfills — its schemas, actor, side-effect/idempotency metadata, evidence
contract, policy action, and graph position. ``NodeDeclaration.from_mapping``
is the typed manifest parser boundary: any key outside the closed schema
below is rejected, which is what keeps a manifest from embedding arbitrary
shell or a general-purpose program.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import NodeId, WorkflowDefinitionId
from enginery.domain.workflow.budget import Budget
from enginery.domain.workflow.schema import IOSchema


class NodeKind(enum.Enum):
    """The eighteen registered node families."""

    NORMALIZE_WORK = "normalize_work"
    REQUEST_HUMAN_DECISION = "request_human_decision"
    EXECUTE_AGENT_TASK = "execute_agent_task"
    RUN_COMMAND = "run_command"
    VERIFY_EVIDENCE = "verify_evidence"
    ROUTE = "route"
    FAN_OUT_AND_JOIN = "fan_out_and_join"
    INVOKE_SUBWORKFLOW = "invoke_subworkflow"
    UPDATE_EXTERNAL_WORK_LEDGER = "update_external_work_ledger"
    CREATE_OR_CLEAN_WORKSPACE = "create_or_clean_workspace"
    STAGE_OR_APPLY_PATCH = "stage_or_apply_patch"
    OPEN_OR_UPDATE_PULL_REQUEST = "open_or_update_pull_request"
    WAIT_FOR_CI = "wait_for_ci"
    MERGE = "merge"
    PREPARE_OR_PUBLISH_RELEASE = "prepare_or_publish_release"
    DEPLOY_OR_ROLL_BACK = "deploy_or_roll_back"
    COMPARE_EVALUATIONS = "compare_evaluations"
    PROMOTE_WORKFLOW = "promote_workflow"


class ActorType(enum.Enum):
    """Who or what executes a node."""

    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    HUMAN = "human"


class SideEffectClass(enum.Enum):
    """Whether a node causes an external side effect."""

    NONE = "none"
    SIDE_EFFECTING = "side_effecting"


class IdempotencyBehavior(enum.Enum):
    """How a side-effecting node achieves idempotency."""

    NOT_APPLICABLE = "not_applicable"
    NATIVE_IDEMPOTENCY_KEY = "native_idempotency_key"
    RECONCILIATION_QUERY = "reconciliation_query"


class BranchOperator(enum.Enum):
    """The closed set of branch-condition comparisons.

    Deliberately not an expression language: a condition compares one
    upstream output field against a fixed set of literal values.
    """

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    ALWAYS = "always"


@dataclass(frozen=True, slots=True)
class BranchCondition:
    """One declarative branch condition: no embedded expression language."""

    operator: BranchOperator
    field_path: str | None = None
    values: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.operator is BranchOperator.ALWAYS:
            if self.field_path is not None or self.values:
                raise InvalidInputError(
                    "an 'always' branch condition cannot declare field or values"
                )
        else:
            if not self.field_path or not self.field_path.strip():
                raise InvalidInputError(
                    f"a {self.operator.value!r} branch condition requires a non-blank field"
                )
            if not self.values:
                raise InvalidInputError(
                    f"a {self.operator.value!r} branch condition requires at least one value"
                )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> BranchCondition:
        allowed_keys = frozenset({"operator", "field", "values"})
        unknown_keys = set(raw) - allowed_keys
        if unknown_keys:
            raise InvalidInputError(
                "branch condition declares unknown keys; manifests cannot embed "
                "executable payloads",
                details={"unknown_keys": sorted(unknown_keys)},
            )
        if "operator" not in raw or not isinstance(raw["operator"], str):
            raise InvalidInputError("branch condition requires a string 'operator'")
        try:
            operator = BranchOperator(raw["operator"])
        except ValueError as error:
            raise InvalidInputError(
                f"branch condition declares unknown operator {raw['operator']!r}"
            ) from error
        raw_field = raw.get("field")
        if raw_field is not None and not isinstance(raw_field, str):
            raise InvalidInputError("branch condition 'field' must be a string")
        raw_values = raw.get("values", ())
        if not isinstance(raw_values, (list, tuple)) or not all(
            isinstance(value, str) for value in raw_values
        ):
            raise InvalidInputError("branch condition 'values' must be a list of strings")
        return cls(operator=operator, field_path=raw_field, values=tuple(raw_values))


_NODE_DECLARATION_KEYS = frozenset(
    {
        "kind",
        "input_schema",
        "output_schema",
        "actor_type",
        "side_effect_class",
        "idempotency_behavior",
        "reconciliation_operation",
        "evidence_contract",
        "emitted_event_types",
        "policy_action",
        "required_capabilities",
        "dependencies",
        "branch_conditions",
        "parallel_group",
        "subworkflow",
        "budget",
    }
)


@dataclass(frozen=True, slots=True)
class NodeDeclaration:
    """The full contract a registered node type fulfills within one manifest."""

    node_id: NodeId
    kind: NodeKind
    input_schema: IOSchema
    output_schema: IOSchema
    actor_type: ActorType
    side_effect_class: SideEffectClass
    idempotency_behavior: IdempotencyBehavior
    reconciliation_operation: str | None = None
    evidence_contract: tuple[str, ...] = field(default_factory=tuple)
    emitted_event_types: tuple[str, ...] = field(default_factory=tuple)
    policy_action: str | None = None
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    dependencies: tuple[NodeId, ...] = field(default_factory=tuple)
    branch_conditions: tuple[BranchCondition, ...] = field(default_factory=tuple)
    parallel_group: str | None = None
    subworkflow: WorkflowDefinitionId | None = None
    budget: Budget = field(default_factory=Budget)

    def __post_init__(self) -> None:
        if self.node_id in self.dependencies:
            raise InvalidInputError(
                "a node cannot depend on itself", details={"node_id": str(self.node_id)}
            )
        if self.side_effect_class is SideEffectClass.NONE:
            if self.idempotency_behavior is not IdempotencyBehavior.NOT_APPLICABLE:
                raise InvalidInputError(
                    "a non-side-effecting node must declare idempotency_behavior=not_applicable",
                    details={"node_id": str(self.node_id)},
                )
            if self.reconciliation_operation is not None:
                raise InvalidInputError(
                    "a non-side-effecting node cannot declare a reconciliation_operation",
                    details={"node_id": str(self.node_id)},
                )
        else:
            if self.idempotency_behavior is IdempotencyBehavior.NOT_APPLICABLE:
                raise InvalidInputError(
                    "a side-effecting node must declare an idempotency behavior "
                    "(undeclared side effects are rejected)",
                    details={"node_id": str(self.node_id)},
                )
            if (
                self.idempotency_behavior is IdempotencyBehavior.RECONCILIATION_QUERY
                and not self.reconciliation_operation
            ):
                raise InvalidInputError(
                    "a reconciliation_query node must declare a non-blank reconciliation_operation",
                    details={"node_id": str(self.node_id)},
                )
        if self.kind is NodeKind.INVOKE_SUBWORKFLOW and self.subworkflow is None:
            raise InvalidInputError(
                "an invoke_subworkflow node must declare a subworkflow",
                details={"node_id": str(self.node_id)},
            )
        if self.kind is not NodeKind.INVOKE_SUBWORKFLOW and self.subworkflow is not None:
            raise InvalidInputError(
                "only an invoke_subworkflow node may declare a subworkflow",
                details={"node_id": str(self.node_id)},
            )

    @classmethod
    def from_mapping(cls, node_id: NodeId, raw: Mapping[str, object]) -> NodeDeclaration:
        unknown_keys = set(raw) - _NODE_DECLARATION_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "node declaration declares unknown keys; manifests cannot embed "
                "executable payloads",
                details={"node_id": str(node_id), "unknown_keys": sorted(unknown_keys)},
            )
        try:
            kind = NodeKind(_require_str(raw, "kind", node_id=node_id))
        except ValueError as error:
            raise InvalidInputError(
                f"node declares unknown kind {raw.get('kind')!r}",
                details={"node_id": str(node_id)},
            ) from error
        input_schema = IOSchema.from_mapping(_require_schema_raw(raw, "input_schema", node_id))
        output_schema = IOSchema.from_mapping(_require_schema_raw(raw, "output_schema", node_id))
        actor_type = ActorType(_require_str(raw, "actor_type", node_id=node_id))
        side_effect_class = SideEffectClass(_require_str(raw, "side_effect_class", node_id=node_id))
        idempotency_behavior = IdempotencyBehavior(
            _require_str(raw, "idempotency_behavior", node_id=node_id)
        )
        reconciliation_operation = _optional_str(raw, "reconciliation_operation")
        subworkflow_raw = raw.get("subworkflow")
        subworkflow = (
            WorkflowDefinitionId(subworkflow_raw) if isinstance(subworkflow_raw, str) else None
        )
        branch_conditions_raw = raw.get("branch_conditions", ())
        if not isinstance(branch_conditions_raw, (list, tuple)):
            raise InvalidInputError("branch_conditions must be a list")
        budget_raw = raw.get("budget", {})
        if not isinstance(budget_raw, Mapping):
            raise InvalidInputError("budget must be a mapping")
        dependencies = tuple(NodeId(value) for value in _require_str_tuple(raw, "dependencies"))
        return cls(
            node_id=node_id,
            kind=kind,
            input_schema=input_schema,
            output_schema=output_schema,
            actor_type=actor_type,
            side_effect_class=side_effect_class,
            idempotency_behavior=idempotency_behavior,
            reconciliation_operation=reconciliation_operation,
            evidence_contract=_require_str_tuple(raw, "evidence_contract"),
            emitted_event_types=_require_str_tuple(raw, "emitted_event_types"),
            policy_action=_optional_str(raw, "policy_action"),
            required_capabilities=_require_str_tuple(raw, "required_capabilities"),
            dependencies=dependencies,
            branch_conditions=tuple(
                BranchCondition.from_mapping(item) for item in branch_conditions_raw
            ),
            parallel_group=_optional_str(raw, "parallel_group"),
            subworkflow=subworkflow,
            budget=Budget.from_mapping(budget_raw),
        )


def _require_str(raw: Mapping[str, object], key: str, *, node_id: NodeId) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"node declaration is missing required non-blank key {key!r}",
            details={"node_id": str(node_id)},
        )
    return value


def _require_schema_raw(
    raw: Mapping[str, object], key: str, node_id: NodeId
) -> Mapping[str, Mapping[str, object]]:
    value = raw.get(key)
    if value is None:
        raise InvalidInputError(
            f"node declaration is missing required key {key!r}",
            details={"node_id": str(node_id)},
        )
    if not isinstance(value, Mapping):
        raise InvalidInputError(
            f"node declaration key {key!r} must be a mapping", details={"node_id": str(node_id)}
        )
    return value


def _require_str_tuple(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key, ())
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise InvalidInputError(f"{key!r} must be a list of strings")
    return tuple(value)


def _optional_str(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInputError(f"{key!r} must be a string")
    return value


__all__ = [
    "ActorType",
    "BranchCondition",
    "BranchOperator",
    "IdempotencyBehavior",
    "NodeDeclaration",
    "NodeKind",
    "SideEffectClass",
]
