"""Tests for enginery.domain.run."""

from __future__ import annotations

import pytest

from enginery.domain.digests import Digest
from enginery.domain.ids import RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import RUN_TRANSITIONS, Run, RunState
from tests.domain.test_state_machine import TestEveryDomainTransitionTableHasNoDeadEnds


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


class TestRunTransitions:
    def test_has_no_dead_ends(self) -> None:
        TestEveryDomainTransitionTableHasNoDeadEnds.assert_every_non_terminal_state_reaches_a_terminal(
            RUN_TRANSITIONS
        )

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (RunState.CREATED, RunState.PREFLIGHT),
            (RunState.PREFLIGHT, RunState.AWAITING_POLICY),
            (RunState.PREFLIGHT, RunState.QUEUED),
            (RunState.PREFLIGHT, RunState.BLOCKED),
            (RunState.PREFLIGHT, RunState.REJECTED),
            (RunState.PREFLIGHT, RunState.SUPERSEDED),
            (RunState.AWAITING_POLICY, RunState.QUEUED),
            (RunState.AWAITING_POLICY, RunState.AWAITING_HUMAN),
            (RunState.AWAITING_POLICY, RunState.REJECTED),
            (RunState.AWAITING_POLICY, RunState.SUPERSEDED),
            (RunState.QUEUED, RunState.RUNNING),
            (RunState.QUEUED, RunState.CANCELLED),
            (RunState.QUEUED, RunState.SUPERSEDED),
            (RunState.RUNNING, RunState.AWAITING_POLICY),
            (RunState.RUNNING, RunState.AWAITING_HUMAN),
            (RunState.RUNNING, RunState.RECONCILING),
            (RunState.RUNNING, RunState.EVIDENCE_VERIFICATION),
            (RunState.RUNNING, RunState.BLOCKED),
            (RunState.RUNNING, RunState.CANCELLED),
            (RunState.RUNNING, RunState.FAILED),
            (RunState.RUNNING, RunState.SUPERSEDED),
            (RunState.AWAITING_HUMAN, RunState.QUEUED),
            (RunState.AWAITING_HUMAN, RunState.RUNNING),
            (RunState.AWAITING_HUMAN, RunState.RECONCILING),
            (RunState.AWAITING_HUMAN, RunState.REJECTED),
            (RunState.AWAITING_HUMAN, RunState.CANCELLED),
            (RunState.AWAITING_HUMAN, RunState.SUPERSEDED),
            (RunState.RECONCILING, RunState.QUEUED),
            (RunState.RECONCILING, RunState.RUNNING),
            (RunState.RECONCILING, RunState.EVIDENCE_VERIFICATION),
            (RunState.RECONCILING, RunState.AWAITING_HUMAN),
            (RunState.RECONCILING, RunState.BLOCKED),
            (RunState.RECONCILING, RunState.FAILED),
            (RunState.RECONCILING, RunState.SUPERSEDED),
            (RunState.EVIDENCE_VERIFICATION, RunState.RUNNING),
            (RunState.EVIDENCE_VERIFICATION, RunState.RECONCILING),
            (RunState.EVIDENCE_VERIFICATION, RunState.AWAITING_HUMAN),
            (RunState.EVIDENCE_VERIFICATION, RunState.SUCCEEDED),
            (RunState.EVIDENCE_VERIFICATION, RunState.BLOCKED),
            (RunState.EVIDENCE_VERIFICATION, RunState.FAILED),
            (RunState.EVIDENCE_VERIFICATION, RunState.SUPERSEDED),
        ],
    )
    def test_every_designed_edge_is_legal(self, source: RunState, target: RunState) -> None:
        assert RUN_TRANSITIONS.allows(source, target)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (RunState.CREATED, RunState.RUNNING),
            (RunState.QUEUED, RunState.AWAITING_HUMAN),
            (RunState.SUCCEEDED, RunState.RUNNING),
            (RunState.BLOCKED, RunState.RUNNING),
        ],
    )
    def test_undesigned_edges_are_illegal(self, source: RunState, target: RunState) -> None:
        assert not RUN_TRANSITIONS.allows(source, target)

    def test_terminal_states_include_blocked_unlike_a_work_item(self) -> None:
        assert RUN_TRANSITIONS.terminal_states == frozenset(
            {
                RunState.SUCCEEDED,
                RunState.BLOCKED,
                RunState.REJECTED,
                RunState.CANCELLED,
                RunState.FAILED,
                RunState.SUPERSEDED,
            }
        )

    def test_superseded_is_directly_reachable_from_every_state_except_created(self) -> None:
        # source divergence can supersede a run from every state where it can
        # occur; "created" precedes the first source-digest check.
        non_created_states = set(RUN_TRANSITIONS.edges) - {RunState.CREATED}
        for source in non_created_states:
            assert RUN_TRANSITIONS.allows(source, RunState.SUPERSEDED), source

    def test_transition_to_advances_state_and_increments_version(self) -> None:
        run = _make_run()

        advanced = run.transition_to(RunState.PREFLIGHT)

        assert advanced.state is RunState.PREFLIGHT
        assert advanced.aggregate_version == 1
        assert run.state is RunState.CREATED

    def test_transition_to_rejects_an_illegal_transition(self) -> None:
        run = _make_run()

        with pytest.raises(Exception, match="illegal transition"):
            run.transition_to(RunState.RUNNING)

    def test_transition_to_rejects_leaving_a_terminal_state(self) -> None:
        run = _make_run(state=RunState.SUCCEEDED)

        with pytest.raises(Exception, match="illegal transition"):
            run.transition_to(RunState.RUNNING)
