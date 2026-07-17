"""Tests for enginery.domain.ids."""

from __future__ import annotations

import pytest

from enginery.domain import ids


@pytest.mark.parametrize(
    "id_type",
    [
        ids.WorkItemId,
        ids.WorkflowDefinitionId,
        ids.RunId,
        ids.NodeId,
        ids.NodeAttemptId,
        ids.ArtifactId,
        ids.PolicyDecisionId,
        ids.InterventionId,
        ids.OutcomeId,
        ids.FactoryChangeId,
    ],
)
class TestIdentifierType:
    def test_accepts_a_plain_token(self, id_type: type) -> None:
        identifier = id_type("wi-000001")

        assert identifier.value == "wi-000001"
        assert str(identifier) == "wi-000001"

    def test_rejects_empty_value(self, id_type: type) -> None:
        with pytest.raises(Exception, match="non-empty"):
            id_type("")

    def test_rejects_whitespace_padding(self, id_type: type) -> None:
        with pytest.raises(Exception, match="whitespace"):
            id_type(" wi-000001 ")

    def test_rejects_overlong_value(self, id_type: type) -> None:
        with pytest.raises(Exception, match="characters"):
            id_type("x" * 129)

    def test_rejects_non_printable_characters(self, id_type: type) -> None:
        with pytest.raises(Exception, match="printable"):
            id_type("wi-000001\x07")

    def test_equality_is_value_based_within_one_type(self, id_type: type) -> None:
        assert id_type("same") == id_type("same")
        assert id_type("same") != id_type("different")

    def test_is_hashable(self, id_type: type) -> None:
        assert len({id_type("a"), id_type("a"), id_type("b")}) == 2

    def test_is_immutable(self, id_type: type) -> None:
        identifier = id_type("wi-000001")
        with pytest.raises(AttributeError):
            identifier.value = "other"


def test_distinct_id_types_holding_the_same_string_are_never_equal() -> None:
    assert ids.WorkItemId("shared") != ids.RunId("shared")  # type: ignore[comparison-overlap]
    assert ids.RunId("shared") != ids.NodeAttemptId("shared")  # type: ignore[comparison-overlap]


class TestOperationId:
    def test_derive_is_deterministic_and_excludes_attempt_number(self) -> None:
        run_id = ids.RunId("run-1")
        node_id = ids.NodeId("node-1")

        first = ids.OperationId.derive(
            run_id=run_id,
            node_id=node_id,
            side_effect_kind="open_or_update_pull_request",
            target_scope="github:org/repo#42",
            ordinal=0,
        )
        retry = ids.OperationId.derive(
            run_id=run_id,
            node_id=node_id,
            side_effect_kind="open_or_update_pull_request",
            target_scope="github:org/repo#42",
            ordinal=0,
        )

        assert first == retry
        assert first.value == retry.value

    def test_derive_signature_has_no_attempt_number_parameter(self) -> None:
        import inspect

        signature = inspect.signature(ids.OperationId.derive)

        assert "attempt" not in signature.parameters
        assert "attempt_number" not in signature.parameters

    @pytest.mark.parametrize(
        "overrides",
        [
            {"node_id": ids.NodeId("node-2")},
            {"side_effect_kind": "merge"},
            {"target_scope": "github:org/repo#43"},
            {"ordinal": 1},
        ],
    )
    def test_derive_changes_identity_when_any_bound_field_changes(
        self, overrides: dict[str, object]
    ) -> None:
        base_kwargs: dict[str, object] = {
            "run_id": ids.RunId("run-1"),
            "node_id": ids.NodeId("node-1"),
            "side_effect_kind": "open_or_update_pull_request",
            "target_scope": "github:org/repo#42",
            "ordinal": 0,
        }
        base = ids.OperationId.derive(**base_kwargs)  # type: ignore[arg-type]
        changed = ids.OperationId.derive(**{**base_kwargs, **overrides})  # type: ignore[arg-type]

        assert base != changed

    def test_derive_rejects_negative_ordinal(self) -> None:
        with pytest.raises(Exception, match="ordinal"):
            ids.OperationId.derive(
                run_id=ids.RunId("run-1"),
                node_id=ids.NodeId("node-1"),
                side_effect_kind="merge",
                target_scope="github:org/repo#42",
                ordinal=-1,
            )

    def test_derive_rejects_blank_side_effect_kind(self) -> None:
        with pytest.raises(Exception, match="side_effect_kind"):
            ids.OperationId.derive(
                run_id=ids.RunId("run-1"),
                node_id=ids.NodeId("node-1"),
                side_effect_kind="  ",
                target_scope="github:org/repo#42",
                ordinal=0,
            )

    def test_derive_rejects_blank_target_scope(self) -> None:
        with pytest.raises(Exception, match="target_scope"):
            ids.OperationId.derive(
                run_id=ids.RunId("run-1"),
                node_id=ids.NodeId("node-1"),
                side_effect_kind="merge",
                target_scope="",
                ordinal=0,
            )
