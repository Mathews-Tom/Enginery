"""Tests for enginery.domain.run."""

from __future__ import annotations

import pytest

from enginery.domain.digests import Digest
from enginery.domain.ids import RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState


def _make_run(**overrides: object) -> Run:
    defaults: dict[str, object] = {
        "id": RunId("run-1"),
        "work_item_id": WorkItemId("wi-1"),
        "work_item_snapshot_digest": Digest.of_bytes(b"snapshot"),
        "workflow_definition_id": WorkflowDefinitionId("wf-1"),
        "workflow_definition_digest": Digest.of_bytes(b"definition"),
        "repository": "org/repo",
        "base_revision": "deadbeef",
        "policy_set_version": "policy-2026-07-17",
        "adapter_versions": {"github": "1.0.0"},
        "capability_lock_digest": Digest.of_bytes(b"capabilities"),
        "environment_manifest_digest": Digest.of_bytes(b"environment"),
        "configuration_snapshot_digest": Digest.of_bytes(b"config"),
        "state": RunState.CREATED,
        "aggregate_version": 0,
    }
    defaults.update(overrides)
    return Run(**defaults)  # type: ignore[arg-type]


class TestRunState:
    def test_has_the_fourteen_designed_states(self) -> None:
        assert {member.value for member in RunState} == {
            "created",
            "preflight",
            "awaiting_policy",
            "queued",
            "running",
            "awaiting_human",
            "reconciling",
            "evidence_verification",
            "succeeded",
            "blocked",
            "rejected",
            "cancelled",
            "failed",
            "superseded",
        }


class TestRun:
    def test_constructs_with_valid_fields(self) -> None:
        run = _make_run()

        assert run.state is RunState.CREATED

    def test_is_immutable(self) -> None:
        run = _make_run()
        with pytest.raises(AttributeError):
            run.repository = "org/other"  # type: ignore[misc]

    @pytest.mark.parametrize("field_name", ["repository", "base_revision", "policy_set_version"])
    def test_rejects_blank_required_fields(self, field_name: str) -> None:
        with pytest.raises(Exception, match="blank"):
            _make_run(**{field_name: "  "})

    def test_rejects_negative_aggregate_version(self) -> None:
        with pytest.raises(Exception, match="aggregate_version"):
            _make_run(aggregate_version=-1)

    def test_rejects_blank_adapter_version_entries(self) -> None:
        with pytest.raises(Exception, match="adapter_versions"):
            _make_run(adapter_versions={"github": "  "})

    def test_adapter_versions_is_defensively_copied_from_the_caller(self) -> None:
        source = {"github": "1.0.0"}
        run = _make_run(adapter_versions=source)
        source["github"] = "9.9.9"

        assert run.adapter_versions["github"] == "1.0.0"

    def test_adapter_versions_cannot_be_mutated_through_the_instance(self) -> None:
        run = _make_run()

        with pytest.raises(TypeError):
            run.adapter_versions["github"] = "9.9.9"  # type: ignore[index]
