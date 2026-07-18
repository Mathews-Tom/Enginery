"""Tests for enginery.domain.serialization: versioned round-trips and golden
compatibility fixtures.

Each golden fixture under ``tests/fixtures/domain/`` is a committed,
schema-versioned JSON envelope. These tests prove two independent things:

1. The current codec still deserializes the exact bytes on disk into the
   exact domain object it was generated from (behavioral compatibility).
2. Re-serializing that object reproduces byte-for-byte the same JSON
   structure (format stability) — so a silent field rename, reordering, or
   type change is caught here rather than at replay time in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from enginery.domain import serialization as ser
from enginery.domain.artifact import Artifact, ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.factory_change import FactoryChange, FactoryChangeState
from enginery.domain.ids import (
    ArtifactId,
    FactoryChangeId,
    InterventionId,
    NodeAttemptId,
    NodeId,
    OutcomeId,
    PolicyDecisionId,
    RunId,
    WorkflowDefinitionId,
    WorkItemId,
)
from enginery.domain.intervention import Intervention, InterventionKind
from enginery.domain.node_attempt import (
    EvidenceResult,
    NodeAttempt,
    NodeAttemptState,
    ReconciliationResult,
)
from enginery.domain.outcome import Outcome, OutcomeKind
from enginery.domain.policy_decision import PolicyAction, PolicyDecision, PolicyResult
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "domain"
_NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)


def _load_fixture(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((_FIXTURES_DIR / f"{name}.json").read_text())
    return result


def _golden_work_item() -> WorkItem:
    return WorkItem(
        id=WorkItemId("wi-golden-1"),
        work_kind=WorkKind.ISSUE,
        source_provider="github",
        external_reference="Mathews-Tom/Enginery#42",
        source_snapshot_reference="a1b2c3d4",
        title="Fix retry budget exhaustion reporting",
        objective="Report the exact evidence bundle when repair budget is exhausted",
        acceptance_criteria=(
            "exhaustion includes failed evidence",
            "exhaustion is a named state",
        ),
        constraints=("no schema break",),
        risk_class=RiskClass.LOW,
        repository_targets=("Mathews-Tom/Enginery",),
        dependencies=(),
        state=WorkItemState.READY,
        aggregate_version=2,
    )


def _golden_run() -> Run:
    return Run(
        id=RunId("run-golden-1"),
        work_item_id=WorkItemId("wi-golden-1"),
        work_item_snapshot_digest=Digest.of_bytes(b"work-item-snapshot"),
        workflow_definition_id=WorkflowDefinitionId("wf-issue-to-pr"),
        workflow_definition_digest=Digest.of_bytes(b"workflow-definition"),
        repository="Mathews-Tom/Enginery",
        base_revision="deadbeefcafefeed",
        policy_set_version="policy-2026-07-17",
        adapter_versions={"github": "1.2.0", "omp": "0.9.1"},
        adapter_fingerprints={
            "github": Digest.of_bytes(b"github adapter"),
            "omp": Digest.of_bytes(b"omp adapter"),
        },
        capability_lock_digest=Digest.of_bytes(b"capability-lock"),
        environment_manifest_digest=Digest.of_bytes(b"environment-manifest"),
        configuration_snapshot_digest=Digest.of_bytes(b"configuration-snapshot"),
        state=RunState.RUNNING,
        aggregate_version=5,
    )


def _golden_node_attempt() -> NodeAttempt:
    return NodeAttempt(
        id=NodeAttemptId("attempt-golden-1"),
        run_id=RunId("run-golden-1"),
        node_id=NodeId("implement"),
        attempt_number=2,
        actor="omp-worker-1",
        input_digest=Digest.of_bytes(b"node-input"),
        state=NodeAttemptState.EVIDENCE_PENDING,
        lease_owner="coordinator-1",
        lease_expires_at=_NOW,
        started_at=_NOW,
        completed_at=None,
        emitted_event_range=(101, 140),
        output_artifact_ids=(ArtifactId("artifact-golden-1"),),
        evidence_result=EvidenceResult.INDETERMINATE,
        cost_amount=0.87,
        duration_seconds=42.5,
        failure_class=None,
        reconciliation_result=ReconciliationResult.FOUND_MATCHING,
        schema_version=1,
    )


def _golden_artifact() -> Artifact:
    return Artifact(
        id=ArtifactId("artifact-golden-1"),
        digest=Digest.of_bytes(b"artifact-bytes"),
        byte_size=2048,
        media_type="text/x-diff",
        kind=ArtifactKind.PATCH,
        run_id=RunId("run-golden-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-golden-1"),
        storage_reference="cas://sha256/deadbeef",
        redaction=RedactionClassification.INTERNAL,
        created_at=_NOW,
        schema_version=1,
    )


def _golden_policy_decision() -> PolicyDecision:
    return PolicyDecision(
        id=PolicyDecisionId("decision-golden-1"),
        action=PolicyAction.PULL_REQUEST_OPEN,
        normalized_inputs={"risk_class": "low", "repository": "Mathews-Tom/Enginery"},
        policy_rule_id="rule-pr-open-low-risk",
        policy_version="policy-2026-07-17",
        result=PolicyResult.ALLOW,
        rationale="low-risk PR open matches auto-allow rule",
        input_digest=Digest.of_bytes(b"policy-input"),
        decided_at=_NOW,
        required_evidence=(),
        required_approver=None,
        superseded=False,
        superseded_by=None,
    )


def _golden_intervention() -> Intervention:
    return Intervention(
        id=InterventionId("intervention-golden-1"),
        kind=InterventionKind.APPROVAL,
        run_id=RunId("run-golden-1"),
        actor="jane@example.com",
        occurred_at=_NOW,
        rationale="approved plan after reviewing acceptance criteria",
        detail={"channel": "cli"},
    )


def _golden_outcome() -> Outcome:
    return Outcome(
        id=OutcomeId("outcome-golden-1"),
        work_item_id=WorkItemId("wi-golden-1"),
        kind=OutcomeKind.PR_ACCEPTED,
        observed_at=_NOW,
        run_id=RunId("run-golden-1"),
        linked_work_item_id=None,
        detail={"pr_number": 42},
        schema_version=1,
    )


def _golden_factory_change() -> FactoryChange:
    return FactoryChange(
        id=FactoryChangeId("fc-golden-1"),
        affected_asset="workflows/issue_to_pr.yaml",
        baseline_version="v3",
        problem_statement="repair budget exhausted on 12% of runs last month",
        hypothesis="raising the repair budget from 2 to 3 reduces exhaustion below 5%",
        candidate_version="v4-candidate",
        state=FactoryChangeState.EVALUATING,
        evaluation_set_digest="sha256:" + "0" * 64,
        comparison_result={"exhaustion_rate_delta": -0.08},
        approval_state=None,
        canary_cohort=(),
        promotion_result=None,
        aggregate_version=2,
    )


_Case = tuple[str, Callable[[], Any], Callable[[Any], dict[str, Any]], Callable[[Any], Any]]
_CASES: list[_Case] = [
    ("work_item", _golden_work_item, ser.work_item_to_dict, ser.work_item_from_dict),
    ("run", _golden_run, ser.run_to_dict, ser.run_from_dict),
    ("node_attempt", _golden_node_attempt, ser.node_attempt_to_dict, ser.node_attempt_from_dict),
    ("artifact", _golden_artifact, ser.artifact_to_dict, ser.artifact_from_dict),
    (
        "policy_decision",
        _golden_policy_decision,
        ser.policy_decision_to_dict,
        ser.policy_decision_from_dict,
    ),
    (
        "intervention",
        _golden_intervention,
        ser.intervention_to_dict,
        ser.intervention_from_dict,
    ),
    ("outcome", _golden_outcome, ser.outcome_to_dict, ser.outcome_from_dict),
    (
        "factory_change",
        _golden_factory_change,
        ser.factory_change_to_dict,
        ser.factory_change_from_dict,
    ),
]


@pytest.mark.parametrize(
    ("name", "build", "to_dict", "from_dict"), _CASES, ids=[case[0] for case in _CASES]
)
class TestGoldenCompatibilityFixtures:
    def test_fixture_deserializes_into_the_exact_golden_object(
        self,
        name: str,
        build: Callable[[], object],
        to_dict: Callable[[object], dict[str, object]],
        from_dict: Callable[[dict[str, object]], object],
    ) -> None:
        fixture = _load_fixture(name)

        assert from_dict(fixture) == build()

    def test_reserializing_the_golden_object_reproduces_the_fixture_exactly(
        self,
        name: str,
        build: Callable[[], object],
        to_dict: Callable[[object], dict[str, object]],
        from_dict: Callable[[dict[str, object]], object],
    ) -> None:
        fixture = _load_fixture(name)

        assert to_dict(build()) == fixture

    def test_round_trip_through_serialize_and_deserialize_is_lossless(
        self,
        name: str,
        build: Callable[[], object],
        to_dict: Callable[[object], dict[str, object]],
        from_dict: Callable[[dict[str, object]], object],
    ) -> None:
        original = build()

        assert from_dict(to_dict(original)) == original

    def test_a_mismatched_schema_version_is_rejected(
        self,
        name: str,
        build: Callable[[], object],
        to_dict: Callable[[object], dict[str, object]],
        from_dict: Callable[[dict[str, object]], object],
    ) -> None:
        payload = to_dict(build())
        payload["schema_version"] = 999

        with pytest.raises(InvalidInputError, match="schema_version"):
            from_dict(payload)

    def test_a_missing_data_envelope_is_rejected(
        self,
        name: str,
        build: Callable[[], object],
        to_dict: Callable[[object], dict[str, object]],
        from_dict: Callable[[dict[str, object]], object],
    ) -> None:
        payload = to_dict(build())
        del payload["data"]

        with pytest.raises(InvalidInputError, match="data"):
            from_dict(payload)


def test_run_v1_fixture_migrates_without_adapter_fingerprints() -> None:
    fixture = _load_fixture("run")
    fixture["schema_version"] = 1
    data = fixture["data"]
    assert isinstance(data, dict)
    del data["adapter_fingerprints"]

    migrated = ser.run_from_dict(fixture)

    assert migrated.adapter_fingerprints == {}


def test_run_with_unbound_adapter_version_cannot_be_persisted() -> None:
    run = _golden_run()
    unbound = replace(run, adapter_fingerprints={})

    with pytest.raises(InvalidInputError, match="must name the same adapters"):
        ser.run_to_dict(unbound)
