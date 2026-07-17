"""Tests for enginery.domain.policy_decision."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.digests import Digest
from enginery.domain.ids import PolicyDecisionId
from enginery.domain.policy_decision import PolicyAction, PolicyDecision, PolicyResult

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_decision(**overrides: object) -> PolicyDecision:
    defaults: dict[str, object] = {
        "id": PolicyDecisionId("decision-1"),
        "action": PolicyAction.PULL_REQUEST_OPEN,
        "normalized_inputs": {"risk_class": "low"},
        "policy_rule_id": "rule-42",
        "policy_version": "policy-2026-07-17",
        "result": PolicyResult.ALLOW,
        "rationale": "matched auto-allow rule for low-risk PR open",
        "input_digest": Digest.of_bytes(b"inputs"),
        "decided_at": _NOW,
    }
    defaults.update(overrides)
    return PolicyDecision(**defaults)  # type: ignore[arg-type]


class TestPolicyAction:
    def test_has_the_seventeen_designed_actions(self) -> None:
        assert {member.value for member in PolicyAction} == {
            "workspace.create",
            "agent.execute",
            "credential.grant",
            "network.request",
            "capability.materialize",
            "evidence.non_applicability.accept",
            "review_finding.waive",
            "pull_request.open",
            "pull_request.merge",
            "release.prepare",
            "release.publish",
            "deployment.execute",
            "deployment.rollback",
            "factory_change.propose",
            "factory_change.canary",
            "factory_change.promote",
            "policy.override",
        }


class TestPolicyResult:
    def test_has_the_three_designed_results(self) -> None:
        assert {member.value for member in PolicyResult} == {
            "allow",
            "deny",
            "require_human",
        }


class TestPolicyDecision:
    def test_constructs_with_valid_fields(self) -> None:
        decision = _make_decision()

        assert decision.result is PolicyResult.ALLOW
        assert decision.superseded is False

    def test_is_immutable(self) -> None:
        decision = _make_decision()
        with pytest.raises(AttributeError):
            decision.result = PolicyResult.DENY  # type: ignore[misc]

    @pytest.mark.parametrize("field_name", ["policy_rule_id", "policy_version", "rationale"])
    def test_rejects_blank_required_fields(self, field_name: str) -> None:
        with pytest.raises(Exception, match="blank"):
            _make_decision(**{field_name: "  "})

    def test_rejects_naive_decided_at(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_decision(decided_at=datetime(2026, 1, 1))

    def test_rejects_superseded_by_without_superseded_flag(self) -> None:
        with pytest.raises(Exception, match="superseded_by"):
            _make_decision(superseded=False, superseded_by=PolicyDecisionId("decision-2"))

    def test_accepts_superseded_by_when_superseded(self) -> None:
        decision = _make_decision(superseded=True, superseded_by=PolicyDecisionId("decision-2"))

        assert decision.superseded_by == PolicyDecisionId("decision-2")

    def test_normalized_inputs_is_defensively_copied_from_the_caller(self) -> None:
        source = {"risk_class": "low"}
        decision = _make_decision(normalized_inputs=source)
        source["risk_class"] = "high"

        assert decision.normalized_inputs["risk_class"] == "low"

    def test_normalized_inputs_cannot_be_mutated_through_the_instance(self) -> None:
        decision = _make_decision()

        with pytest.raises(TypeError):
            decision.normalized_inputs["risk_class"] = "high"  # type: ignore[index]
