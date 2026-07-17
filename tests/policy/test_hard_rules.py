from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator, PolicyRule
from enginery.policy.rules import HardRuleError
from enginery.policy.schemas import ApprovalSchema


def _agent() -> AuthorityPrincipal:
    return AuthorityPrincipal("run-7", PrincipalType.AGENT, "worker", "fixture")


def _human(identifier: str) -> AuthorityPrincipal:
    return AuthorityPrincipal(identifier, PrincipalType.HUMAN, "operator", "fixture")


def _evaluator(registry: ApprovalRegistry) -> PolicyEvaluator:
    return PolicyEvaluator(policy_version="1.0.0", approval_registry=registry)


def test_one_registered_human_can_approve_agent_produced_non_applicability() -> None:
    agent = _agent()
    operator = _human("operator-1")
    schema = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        target_resource="criterion-2",
    )
    registry = ApprovalRegistry((operator,))

    registry.record_approval(schema, (operator,))
    decision = _evaluator(registry).evaluate(schema)

    assert decision.result is PolicyResult.ALLOW


def test_producer_cannot_approve_own_request() -> None:
    operator = _human("operator-1")
    schema = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=operator.id,
        producer_principal_ids=(operator.id,),
        target_resource="criterion-2",
    )

    with pytest.raises(HardRuleError, match="producer separation"):
        ApprovalRegistry((operator,)).record_approval(schema, (operator,))


def test_worker_cannot_approve_human_only_action() -> None:
    producer = _agent()
    worker = AuthorityPrincipal("run-8", PrincipalType.AGENT, "worker", "fixture")
    schema = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=producer.id,
        producer_principal_ids=(producer.id,),
        target_resource="criterion-2",
    )

    with pytest.raises(HardRuleError, match="interactive human"):
        ApprovalRegistry().record_approval(schema, (worker,))


def test_single_human_deployment_blocks_dual_human_action() -> None:
    agent = _agent()
    operator = _human("operator-1")
    schema = ApprovalSchema(
        action=PolicyAction.FACTORY_CHANGE_CANARY,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        target_resource="candidate-1",
    )
    registry = ApprovalRegistry((operator,))

    decision = _evaluator(registry).evaluate(schema)

    assert decision.result is PolicyResult.DENY
    assert decision.policy_rule_id == "dual_human_unavailable"
    with pytest.raises(HardRuleError, match="dual-human separation"):
        registry.record_approval(schema, (operator,))


def test_two_registered_humans_can_approve_dual_human_action() -> None:
    agent = _agent()
    first = _human("operator-1")
    second = _human("operator-2")
    schema = ApprovalSchema(
        action=PolicyAction.FACTORY_CHANGE_CANARY,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        target_resource="candidate-1",
        canary_target="non_production_shadow",
    )
    registry = ApprovalRegistry((first, second))

    registry.record_approval(schema, (first, second))

    assert _evaluator(registry).evaluate(schema).result is PolicyResult.ALLOW


def test_changed_digest_supersedes_prior_decision() -> None:
    agent = _agent()
    operator = _human("operator-1")
    first = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        target_resource="criterion-1",
    )
    second = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        target_resource="criterion-2",
    )
    registry = ApprovalRegistry((operator,))

    first_record = registry.record_approval(first, (operator,), request_id="request-1")
    registry.record_approval(second, (operator,), request_id="request-1")

    assert first_record.superseded is False
    assert registry.get_approval(first) is None
    assert _evaluator(registry).evaluate(first).result is PolicyResult.REQUIRE_HUMAN
    assert _evaluator(registry).evaluate(second).result is PolicyResult.ALLOW


def test_expired_approval_cannot_authorize_action() -> None:
    agent = _agent()
    operator = _human("operator-1")
    now = datetime.now(UTC)
    schema = ApprovalSchema(
        action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        target_resource="criterion-2",
    )
    registry = ApprovalRegistry((operator,))

    registry.record_approval(
        schema,
        (operator,),
        decided_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
    )

    assert _evaluator(registry).evaluate(schema).result is PolicyResult.REQUIRE_HUMAN


@pytest.mark.parametrize(
    ("schema", "expected_result"),
    [
        (
            ApprovalSchema(
                action=PolicyAction.CREDENTIAL_GRANT,
                credential_delivery_target="fixed_broker",
                agent_authored_executable=False,
            ),
            PolicyResult.ALLOW,
        ),
        (
            ApprovalSchema(
                action=PolicyAction.CREDENTIAL_GRANT,
                credential_delivery_target="agent_workspace",
            ),
            PolicyResult.DENY,
        ),
        (
            ApprovalSchema(
                action=PolicyAction.NETWORK_REQUEST,
                retry_after_ambiguous_effect=True,
                reconciliation_complete=True,
            ),
            PolicyResult.ALLOW,
        ),
        (
            ApprovalSchema(
                action=PolicyAction.NETWORK_REQUEST,
                retry_after_ambiguous_effect=True,
                reconciliation_complete=False,
            ),
            PolicyResult.DENY,
        ),
        (
            ApprovalSchema(
                action=PolicyAction.FACTORY_CHANGE_PROPOSE,
                mutates_active_factory_asset=False,
                candidate_received_held_out_input=False,
            ),
            PolicyResult.ALLOW,
        ),
        (
            ApprovalSchema(
                action=PolicyAction.FACTORY_CHANGE_PROPOSE,
                candidate_received_held_out_input=True,
            ),
            PolicyResult.DENY,
        ),
    ],
)
def test_hard_rule_boundaries(
    schema: ApprovalSchema,
    expected_result: PolicyResult,
) -> None:
    rule = PolicyRule(
        id="allow_test_action",
        action=schema.action,
        result=PolicyResult.ALLOW,
        rationale="Test-only explicit rule.",
    )

    decision = PolicyEvaluator(policy_version="1.0.0", rules=(rule,)).evaluate(schema)

    assert decision.result is expected_result


def test_hard_rule_override_scope_is_never_authorized() -> None:
    agent = _agent()
    operator = _human("operator-1")
    schema = ApprovalSchema(
        action=PolicyAction.POLICY_OVERRIDE,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        override_reason="operator investigation",
        override_scope=("evidence.hard_required",),
        override_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    registry = ApprovalRegistry((operator,))

    decision = _evaluator(registry).evaluate(schema)

    assert decision.result is PolicyResult.DENY
    assert decision.policy_rule_id == "hard_rule"


def test_run_introduced_capability_requires_exact_digest_approval() -> None:
    agent = _agent()
    operator = _human("operator-1")
    approved = ApprovalSchema(
        action=PolicyAction.CAPABILITY_MATERIALIZE,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        requested_capability="formatter",
        diff_or_artifact_digest="sha256:approved",
        capability_introduced_by_run=True,
    )
    changed = ApprovalSchema(
        action=PolicyAction.CAPABILITY_MATERIALIZE,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        requested_capability="formatter",
        diff_or_artifact_digest="sha256:changed",
        capability_introduced_by_run=True,
    )
    registry = ApprovalRegistry((operator,))

    assert _evaluator(registry).evaluate(approved).result is PolicyResult.REQUIRE_HUMAN
    registry.record_approval(approved, (operator,))

    assert _evaluator(registry).evaluate(approved).result is PolicyResult.ALLOW
    assert _evaluator(registry).evaluate(changed).result is PolicyResult.REQUIRE_HUMAN


def test_soft_policy_override_requires_and_accepts_independent_human() -> None:
    agent = _agent()
    operator = _human("operator-1")
    schema = ApprovalSchema(
        action=PolicyAction.POLICY_OVERRIDE,
        requesting_principal_id=agent.id,
        producer_principal_ids=(agent.id,),
        override_reason="temporary concurrency reduction",
        override_scope=("concurrency_limit",),
        override_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    registry = ApprovalRegistry((operator,))

    assert _evaluator(registry).evaluate(schema).result is PolicyResult.REQUIRE_HUMAN
    registry.record_approval(schema, (operator,))

    assert _evaluator(registry).evaluate(schema).result is PolicyResult.ALLOW
