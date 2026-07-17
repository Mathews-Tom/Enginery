"""Tests for enginery.domain.node_attempt."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.domain.node_attempt import (
    EvidenceResult,
    NodeAttempt,
    NodeAttemptState,
    ReconciliationResult,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_attempt(**overrides: object) -> NodeAttempt:
    defaults: dict[str, object] = {
        "id": NodeAttemptId("attempt-1"),
        "run_id": RunId("run-1"),
        "node_id": NodeId("node-1"),
        "attempt_number": 1,
        "actor": "worker-1",
        "input_digest": Digest.of_bytes(b"input"),
        "state": NodeAttemptState.PENDING,
    }
    defaults.update(overrides)
    return NodeAttempt(**defaults)  # type: ignore[arg-type]


class TestNodeAttemptState:
    def test_has_the_ten_designed_states(self) -> None:
        assert {member.value for member in NodeAttemptState} == {
            "pending",
            "leased",
            "running",
            "reconciling",
            "output_pending",
            "evidence_pending",
            "passed",
            "failed",
            "cancelled",
            "timed_out",
        }


class TestReconciliationResult:
    def test_has_the_four_designed_results(self) -> None:
        assert {member.value for member in ReconciliationResult} == {
            "not_found",
            "found_matching",
            "found_conflicting",
            "indeterminate",
        }


class TestEvidenceResult:
    def test_has_the_three_designed_results(self) -> None:
        assert {member.value for member in EvidenceResult} == {"pass", "fail", "indeterminate"}


class TestNodeAttempt:
    def test_constructs_with_minimal_fields(self) -> None:
        attempt = _make_attempt()

        assert attempt.attempt_number == 1
        assert attempt.lease_owner is None
        assert attempt.output_artifact_ids == ()
        assert attempt.schema_version == 1

    def test_constructs_with_full_fields(self) -> None:
        attempt = _make_attempt(
            lease_owner="coordinator-1",
            lease_expires_at=_NOW,
            started_at=_NOW,
            completed_at=_NOW,
            emitted_event_range=(1, 10),
            output_artifact_ids=(ArtifactId("artifact-1"),),
            evidence_result=EvidenceResult.PASS,
            cost_amount=0.42,
            duration_seconds=12.5,
            failure_class=FailureClass.TIMEOUT,
            reconciliation_result=ReconciliationResult.FOUND_MATCHING,
        )

        assert attempt.evidence_result is EvidenceResult.PASS
        assert attempt.failure_class is FailureClass.TIMEOUT

    def test_is_immutable(self) -> None:
        attempt = _make_attempt()
        with pytest.raises(AttributeError):
            attempt.state = NodeAttemptState.RUNNING  # type: ignore[misc]

    def test_rejects_attempt_number_below_one(self) -> None:
        with pytest.raises(Exception, match="attempt_number"):
            _make_attempt(attempt_number=0)

    def test_rejects_blank_actor(self) -> None:
        with pytest.raises(Exception, match="actor"):
            _make_attempt(actor="  ")

    def test_rejects_naive_lease_expiry(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_attempt(lease_expires_at=datetime(2026, 1, 1))

    def test_rejects_naive_started_at(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_attempt(started_at=datetime(2026, 1, 1))

    def test_rejects_inverted_emitted_event_range(self) -> None:
        with pytest.raises(Exception, match="emitted_event_range"):
            _make_attempt(emitted_event_range=(10, 1))

    def test_rejects_negative_emitted_event_range_start(self) -> None:
        with pytest.raises(Exception, match="emitted_event_range"):
            _make_attempt(emitted_event_range=(-1, 1))

    def test_rejects_negative_cost_amount(self) -> None:
        with pytest.raises(Exception, match="cost_amount"):
            _make_attempt(cost_amount=-0.01)

    def test_rejects_negative_duration(self) -> None:
        with pytest.raises(Exception, match="duration_seconds"):
            _make_attempt(duration_seconds=-1.0)

    def test_rejects_schema_version_below_one(self) -> None:
        with pytest.raises(Exception, match="schema_version"):
            _make_attempt(schema_version=0)
