"""Execute generated authority-bypass attempts against the policy layer."""

from __future__ import annotations

import random
import secrets
import string
from collections.abc import Callable
from functools import partial

from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator, PolicyRule
from enginery.policy.rules import HardRuleError
from enginery.policy.schemas import ApprovalSchema


def _token(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(20))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _expect_hard_rule(operation: Callable[[], object]) -> None:
    try:
        operation()
    except HardRuleError:
        return
    raise RuntimeError("unsafe authority request was accepted")


def run_gate() -> None:
    seed = secrets.randbits(64)
    rng = random.Random(seed)
    cases = 0
    for _ in range(32):
        token = _token(rng)
        producer = AuthorityPrincipal(
            f"run-{token}",
            PrincipalType.AGENT,
            "worker",
            "generated-fixture",
        )
        first_human = AuthorityPrincipal(
            f"human-a-{token}",
            PrincipalType.HUMAN,
            "operator",
            "generated-fixture",
        )
        second_human = AuthorityPrincipal(
            f"human-b-{token}",
            PrincipalType.HUMAN,
            "operator",
            "generated-fixture",
        )
        single_operator_registry = ApprovalRegistry((first_human,))
        evaluator = PolicyEvaluator("generated-policy", approval_registry=single_operator_registry)
        dual_human_schema = ApprovalSchema(
            action=PolicyAction.FACTORY_CHANGE_CANARY,
            requesting_principal_id=producer.id,
            producer_principal_ids=(producer.id,),
            target_resource=f"candidate-{token}",
            candidate_affects_protected_control=False,
            canary_target=f"cohort-{token}",
        )
        _assert(
            evaluator.evaluate(dual_human_schema).result is PolicyResult.DENY,
            "single-principal deployment executed a dual-human action",
        )
        _expect_hard_rule(
            partial(
                single_operator_registry.record_approval,
                dual_human_schema,
                (first_human,),
            )
        )

        producer_separation_schema = ApprovalSchema(
            action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
            requesting_principal_id=producer.id,
            producer_principal_ids=(producer.id,),
            target_resource=f"criterion-{token}",
        )
        single_operator_registry.record_approval(producer_separation_schema, (first_human,))
        _assert(
            evaluator.evaluate(producer_separation_schema).result is PolicyResult.ALLOW,
            "single human could not approve run-produced output",
        )
        self_approval_schema = ApprovalSchema(
            action=PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
            requesting_principal_id=first_human.id,
            producer_principal_ids=(first_human.id,),
            target_resource=f"self-{token}",
        )
        _expect_hard_rule(
            partial(
                single_operator_registry.record_approval,
                self_approval_schema,
                (first_human,),
            )
        )

        unknown = evaluator.explain_action_name(f"generated.{token}")
        _assert(unknown.result is PolicyResult.DENY, "unknown action was not denied")

        credential_rule = PolicyRule(
            id=f"credential-{token}",
            action=PolicyAction.CREDENTIAL_GRANT,
            result=PolicyResult.ALLOW,
            rationale="generated allow boundary",
        )
        credential_evaluator = PolicyEvaluator("generated-policy", rules=(credential_rule,))
        broker_request = ApprovalSchema(
            action=PolicyAction.CREDENTIAL_GRANT,
            credential_delivery_target="fixed_broker",
            agent_authored_executable=False,
        )
        leaked_request = ApprovalSchema(
            action=PolicyAction.CREDENTIAL_GRANT,
            credential_delivery_target=f"agent-workspace-{token}",
        )
        _assert(
            credential_evaluator.evaluate(broker_request).result is PolicyResult.ALLOW,
            "fixed broker boundary was not allowed",
        )
        _assert(
            credential_evaluator.evaluate(leaked_request).result is PolicyResult.DENY,
            "credential leakage bypass was allowed",
        )

        capability_schema = ApprovalSchema(
            action=PolicyAction.CAPABILITY_MATERIALIZE,
            requesting_principal_id=producer.id,
            producer_principal_ids=(producer.id,),
            requested_capability=f"capability-{token}",
            diff_or_artifact_digest=f"digest-{token}",
            capability_introduced_by_run=True,
        )
        single_operator_registry.record_approval(capability_schema, (first_human,))
        _assert(
            evaluator.evaluate(capability_schema).result is PolicyResult.ALLOW,
            "exact capability approval was not honored",
        )
        changed_capability_schema = ApprovalSchema(
            action=PolicyAction.CAPABILITY_MATERIALIZE,
            requesting_principal_id=producer.id,
            producer_principal_ids=(producer.id,),
            requested_capability=f"capability-{token}",
            diff_or_artifact_digest=f"changed-{token}",
            capability_introduced_by_run=True,
        )
        _assert(
            evaluator.evaluate(changed_capability_schema).result is PolicyResult.REQUIRE_HUMAN,
            "changed capability reused a prior approval",
        )
        dual_registry = ApprovalRegistry((first_human, second_human))
        dual_registry.record_approval(dual_human_schema, (first_human, second_human))
        _assert(
            PolicyEvaluator("generated-policy", approval_registry=dual_registry)
            .evaluate(dual_human_schema)
            .result
            is PolicyResult.ALLOW,
            "two registered humans could not approve a dual-human action",
        )
        cases += 1
    print(f"PASS policy authority adversarial cases={cases} seed={seed}")


if __name__ == "__main__":
    run_gate()
