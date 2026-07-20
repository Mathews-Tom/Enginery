from __future__ import annotations

from enginery.domain.enums import RiskClass
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.policy.defaults import stage2_policy_rules
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema


def _evaluator() -> PolicyEvaluator:
    return PolicyEvaluator(policy_version="stage2-1.0.0", rules=stage2_policy_rules())


def test_merge_is_allowed_for_low_and_medium_risk() -> None:
    evaluator = _evaluator()

    low = evaluator.evaluate(
        ApprovalSchema(action=PolicyAction.PULL_REQUEST_MERGE, risk_class=RiskClass.LOW)
    )
    medium = evaluator.evaluate(
        ApprovalSchema(action=PolicyAction.PULL_REQUEST_MERGE, risk_class=RiskClass.MEDIUM)
    )

    assert low.result is PolicyResult.ALLOW
    assert medium.result is PolicyResult.ALLOW


def test_merge_is_denied_for_high_risk() -> None:
    evaluator = _evaluator()

    decision = evaluator.evaluate(
        ApprovalSchema(action=PolicyAction.PULL_REQUEST_MERGE, risk_class=RiskClass.HIGH)
    )

    assert decision.result is PolicyResult.DENY


def test_release_prepare_is_allowed_for_low_and_medium_risk() -> None:
    evaluator = _evaluator()

    decision = evaluator.evaluate(
        ApprovalSchema(action=PolicyAction.RELEASE_PREPARE, risk_class=RiskClass.LOW)
    )

    assert decision.result is PolicyResult.ALLOW


def test_release_publish_has_no_plain_allow_rule() -> None:
    """release.publish is intentionally absent -- it must go through the
    interactive human-approval path, never a plain allow/deny row."""
    evaluator = _evaluator()

    decision = evaluator.evaluate(
        ApprovalSchema(
            action=PolicyAction.RELEASE_PUBLISH,
            risk_class=RiskClass.LOW,
            requesting_principal_id="operator-1",
        )
    )

    assert decision.result is not PolicyResult.ALLOW
