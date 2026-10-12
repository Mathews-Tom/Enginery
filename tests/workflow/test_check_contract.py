"""Behavior tests for typed checks bound to executable workflow nodes."""

from __future__ import annotations

import copy
from collections.abc import Mapping

import pytest

from enginery.domain import serialization
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import NodeId
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.workflow.check import (
    CheckDeclaration,
    CheckDisposition,
    CheckObserver,
    CheckSubjectKind,
    CommandSource,
)
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.domain.workflow.node import NodeDeclaration
from enginery.workflows.issue_to_pr import (
    STAGE1_VALIDATE_CHECK_ID,
    STAGE1_VERIFY_CHECK_ID,
    stage1_work_manifest,
)


def _command_check(**overrides: object) -> dict[str, object]:
    check: dict[str, object] = {
        "id": "unit.lint",
        "subject": "workspace_head",
        "command": {"source": "declared", "argv": ["ruff", "check", "."]},
        "on_fail": "repair_or_stop",
        "on_indeterminate": "stop",
    }
    check.update(overrides)
    return check


def _node(kind: str, **extra: object) -> dict[str, object]:
    node: dict[str, object] = {
        "kind": kind,
        "input_schema": {},
        "output_schema": {},
        "actor_type": "deterministic",
        "side_effect_class": "none",
        "idempotency_behavior": "not_applicable",
        "dependencies": [],
    }
    node.update(extra)
    return node


def _manifest(nodes: Mapping[str, object], *, schema_version: int = 2) -> WorkflowManifest:
    return WorkflowManifest.from_mapping(
        {
            "id": "check-test",
            "name": "check-test",
            "schema_version": schema_version,
            "nodes": dict(nodes),
            "terminal_states": ["done"],
            "terminal_state_mapping": {},
        }
    )


class TestCheckBinding:
    def test_run_command_without_a_check_is_rejected(self) -> None:
        with pytest.raises(InvalidInputError, match="must bind a check"):
            _manifest({"validate": _node("run_command")})

    def test_verify_evidence_without_a_check_is_rejected(self) -> None:
        with pytest.raises(InvalidInputError, match="must bind a check"):
            _manifest({"verify": _node("verify_evidence")})

    def test_a_check_on_another_kind_is_rejected(self) -> None:
        with pytest.raises(InvalidInputError, match="only run_command and verify_evidence"):
            _manifest({"route": _node("route", check=_command_check())})

    def test_bound_command_check_is_accepted_and_round_trips(self) -> None:
        manifest = _manifest({"validate": _node("run_command", check=_command_check())})

        decoded = serialization.workflow_manifest_from_dict(
            serialization.workflow_manifest_to_dict(manifest)
        )

        check = decoded.nodes[NodeId("validate")].check
        assert check is not None
        assert check.command is not None
        assert check.command.argv == ("ruff", "check", ".")
        assert decoded.content_digest == manifest.content_digest

    def test_duplicate_check_ids_are_rejected(self) -> None:
        with pytest.raises(InvalidInputError, match="unique"):
            _manifest(
                {
                    "a": _node("run_command", check=_command_check()),
                    "b": _node("run_command", check=_command_check()),
                }
            )

    def test_legacy_schema_manifest_without_checks_still_decodes_unchanged(self) -> None:
        legacy = _manifest({"validate": _node("run_command")}, schema_version=1)

        payload = serialization.workflow_manifest_to_dict(legacy)
        decoded = serialization.workflow_manifest_from_dict(payload)

        assert decoded.nodes[NodeId("validate")].check is None
        assert "check" not in payload["data"]["nodes"]["validate"]  # type: ignore[index]
        assert decoded.content_digest == legacy.content_digest


class TestCheckShape:
    def test_command_and_observer_are_mutually_exclusive(self) -> None:
        with pytest.raises(InvalidInputError, match="exactly one"):
            CheckDeclaration.from_mapping(_command_check(observer="pull_request_merge_readiness"))

    def test_a_check_with_neither_command_nor_observer_is_rejected(self) -> None:
        raw = _command_check()
        del raw["command"]
        with pytest.raises(InvalidInputError, match="exactly one"):
            CheckDeclaration.from_mapping(raw)

    def test_unknown_check_keys_are_rejected(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown keys"):
            CheckDeclaration.from_mapping(_command_check(script="rm -rf /"))

    def test_unknown_command_keys_are_rejected(self) -> None:
        with pytest.raises(InvalidInputError, match="unknown keys"):
            CheckDeclaration.from_mapping(
                _command_check(command={"source": "declared", "argv": ["x"], "shell": True})
            )

    def test_unknown_observer_is_rejected(self) -> None:
        raw = _command_check(observer="anything_goes")
        del raw["command"]
        with pytest.raises(InvalidInputError, match="unknown value"):
            CheckDeclaration.from_mapping(raw)

    @pytest.mark.parametrize(
        "argv",
        [["ruff check ."], ["bash", "-c", "ruff check ."], ["/bin/sh", "-c", "x"], [], [" "]],
    )
    def test_shell_strings_and_empty_argv_are_rejected(self, argv: list[str]) -> None:
        with pytest.raises(InvalidInputError):
            CheckDeclaration.from_mapping(
                _command_check(command={"source": "declared", "argv": argv})
            )

    def test_request_bound_command_declares_no_argv(self) -> None:
        check = CheckDeclaration.from_mapping(_command_check(command={"source": "request"}))
        assert check.command is not None
        assert check.command.source is CommandSource.REQUEST
        with pytest.raises(InvalidInputError, match="cannot declare argv"):
            CheckDeclaration.from_mapping(
                _command_check(command={"source": "request", "argv": ["pytest"]})
            )

    @pytest.mark.parametrize("check_id", ["Lint", "lint", "unit lint", "unit.", ".unit", ""])
    def test_check_ids_must_be_stable_dotted_identifiers(self, check_id: str) -> None:
        with pytest.raises(InvalidInputError, match="check id"):
            CheckDeclaration.from_mapping(_command_check(id=check_id))

    def test_a_non_pass_behavior_cannot_be_proceed(self) -> None:
        with pytest.raises(InvalidInputError, match="on_fail"):
            CheckDeclaration.from_mapping(_command_check(on_fail="proceed"))
        with pytest.raises(InvalidInputError, match="on_indeterminate"):
            CheckDeclaration.from_mapping(_command_check(on_indeterminate="proceed"))
        with pytest.raises(InvalidInputError, match="on_indeterminate"):
            CheckDeclaration.from_mapping(_command_check(on_indeterminate="repair_or_stop"))


class TestNonPassNeverProceeds:
    @pytest.mark.parametrize(
        ("on_fail", "on_indeterminate"),
        [
            ("repair_or_stop", "stop"),
            ("repair_or_stop", "hold"),
            ("stop", "stop"),
            ("stop", "hold"),
        ],
    )
    def test_only_pass_proceeds(self, on_fail: str, on_indeterminate: str) -> None:
        check = CheckDeclaration.from_mapping(
            _command_check(on_fail=on_fail, on_indeterminate=on_indeterminate)
        )

        assert check.disposition(EvidenceResult.PASS) is CheckDisposition.PROCEED
        assert check.disposition(EvidenceResult.FAIL) is CheckDisposition(on_fail)
        assert check.disposition(EvidenceResult.INDETERMINATE) is CheckDisposition(on_indeterminate)
        for result in (EvidenceResult.FAIL, EvidenceResult.INDETERMINATE):
            assert check.disposition(result) is not CheckDisposition.PROCEED


class TestStage1ManifestChecks:
    def test_validate_binds_a_request_bound_command_at_the_workspace_head(self) -> None:
        check = stage1_work_manifest().nodes[NodeId("validate")].check

        assert check is not None
        assert check.check_id == STAGE1_VALIDATE_CHECK_ID
        assert check.subject is CheckSubjectKind.WORKSPACE_HEAD
        assert check.command is not None
        assert check.command.source is CommandSource.REQUEST
        assert check.observer is None
        assert check.disposition(EvidenceResult.FAIL) is CheckDisposition.REPAIR_OR_STOP
        assert check.disposition(EvidenceResult.INDETERMINATE) is CheckDisposition.STOP

    def test_verify_binds_the_merge_readiness_observer_at_the_pull_request_head(self) -> None:
        check = stage1_work_manifest().nodes[NodeId("verify")].check

        assert check is not None
        assert check.check_id == STAGE1_VERIFY_CHECK_ID
        assert check.subject is CheckSubjectKind.PULL_REQUEST_HEAD
        assert check.observer is CheckObserver.PULL_REQUEST_MERGE_READINESS
        assert check.command is None
        assert check.disposition(EvidenceResult.INDETERMINATE) is CheckDisposition.HOLD

    def test_only_executable_nodes_bind_checks(self) -> None:
        manifest = stage1_work_manifest()

        bound = {str(node_id) for node_id, node in manifest.nodes.items() if node.check}

        assert bound == {"validate", "verify"}

    def test_stage1_manifest_round_trips_with_a_stable_digest(self) -> None:
        manifest = stage1_work_manifest()

        decoded = serialization.workflow_manifest_from_dict(
            serialization.workflow_manifest_to_dict(manifest)
        )

        assert decoded == manifest
        assert decoded.content_digest == manifest.content_digest

    def test_a_persisted_pre_check_stage1_manifest_still_decodes_with_its_original_digest(
        self,
    ) -> None:
        current = stage1_work_manifest().to_mapping()
        legacy = copy.deepcopy(current)
        legacy["schema_version"] = 1
        for node in legacy["nodes"].values():  # type: ignore[attr-defined]
            node.pop("check", None)

        decoded = serialization.workflow_manifest_from_dict({"schema_version": 1, "data": legacy})

        assert decoded.schema_version == 1
        assert decoded.to_mapping() == legacy
        assert decoded.content_digest != stage1_work_manifest().content_digest

    def test_node_declaration_rejects_a_check_on_a_non_executable_kind(self) -> None:
        with pytest.raises(InvalidInputError, match="only run_command and verify_evidence"):
            NodeDeclaration.from_mapping(NodeId("n"), _node("route", check=_command_check()))
