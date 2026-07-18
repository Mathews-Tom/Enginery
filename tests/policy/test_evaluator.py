from enginery.domain.enums import RiskClass
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.policy.evaluator import PolicyEvaluator, PolicyRule
from enginery.policy.schemas import ApprovalSchema


def test_default_deny_evaluation() -> None:
    schema = ApprovalSchema(action=PolicyAction.WORKSPACE_CREATE)

    decision = PolicyEvaluator(policy_version="1.0.0").evaluate(schema)

    assert decision.result is PolicyResult.DENY
    assert decision.rationale == "Default deny: no matching policy rule found."
    assert decision.action is PolicyAction.WORKSPACE_CREATE


def test_schema_digest_binds_explicit_nulls_and_empty_collections() -> None:
    null_paths = ApprovalSchema(action=PolicyAction.WORKSPACE_CREATE)
    empty_paths = ApprovalSchema(action=PolicyAction.WORKSPACE_CREATE, changed_paths=())

    canonical = null_paths.canonical_inputs()

    assert canonical["changed_paths"] is None
    assert canonical["producer_principal_ids"] is None
    assert canonical["override_scope"] is None
    assert null_paths.digest() != empty_paths.digest()


def test_matching_exact_action_rule_can_allow_requested_risk() -> None:
    rule = PolicyRule(
        id="allow_low_workspace",
        action=PolicyAction.WORKSPACE_CREATE,
        result=PolicyResult.ALLOW,
        rationale="A low-risk workspace is permitted.",
        risk_classes=frozenset({RiskClass.LOW}),
    )
    evaluator = PolicyEvaluator(policy_version="1.0.0", rules=(rule,))

    allowed = evaluator.evaluate(
        ApprovalSchema(action=PolicyAction.WORKSPACE_CREATE, risk_class=RiskClass.LOW)
    )
    denied = evaluator.evaluate(
        ApprovalSchema(action=PolicyAction.WORKSPACE_CREATE, risk_class=RiskClass.HIGH)
    )

    assert allowed.result is PolicyResult.ALLOW
    assert denied.result is PolicyResult.DENY


def test_unknown_action_name_is_denied_without_action_coercion() -> None:
    explanation = PolicyEvaluator(policy_version="1.0.0").explain_action_name("drop.database")

    assert explanation.result is PolicyResult.DENY
    assert explanation.rule_id == "unknown_action"
