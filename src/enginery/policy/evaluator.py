"""Closed policy evaluation with hard-rule enforcement and human authority."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from enginery.domain.enums import RiskClass
from enginery.domain.ids import PolicyDecisionId
from enginery.domain.policy_decision import PolicyAction, PolicyDecision, PolicyResult
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.rules import HardRuleEnforcer, HardRuleError
from enginery.policy.schemas import ApprovalSchema


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One exact action rule; omitted actions remain denied."""

    id: str
    action: PolicyAction
    result: PolicyResult
    rationale: str
    risk_classes: frozenset[RiskClass] | None = None

    def matches(self, schema: ApprovalSchema) -> bool:
        return self.action is schema.action and (
            self.risk_classes is None or schema.risk_class in self.risk_classes
        )


@dataclass(frozen=True, slots=True)
class PolicyExplanation:
    """A non-authorizing policy explanation, including unknown-action denial."""

    action: str
    result: PolicyResult
    rule_id: str
    rationale: str
    normalized_inputs: Mapping[str, object] | None


class PolicyEvaluator:
    """Evaluate exact rules after the closed hard-rule layer has run."""

    def __init__(
        self,
        policy_version: str,
        rules: Iterable[PolicyRule] = (),
        approval_registry: ApprovalRegistry | None = None,
    ) -> None:
        if not policy_version.strip():
            raise ValueError("policy_version must be non-blank")
        self._policy_version = policy_version
        self._rules = tuple(rules)
        self._approval_registry = approval_registry

    def evaluate(self, schema: ApprovalSchema) -> PolicyDecision:
        """Return a durable allow, deny, or human-required decision."""

        try:
            HardRuleEnforcer.enforce_request(schema)
        except HardRuleError as error:
            return self._decision(
                schema,
                result=PolicyResult.DENY,
                rule_id="hard_rule",
                rationale=str(error),
            )
        if HardRuleEnforcer.requires_human(schema):
            return self._evaluate_human_required(schema)
        matching_rule = next((rule for rule in self._rules if rule.matches(schema)), None)
        if matching_rule is None:
            return self._decision(
                schema,
                result=PolicyResult.DENY,
                rule_id="default_deny",
                rationale="Default deny: no matching policy rule found.",
            )
        return self._decision(
            schema,
            result=matching_rule.result,
            rule_id=matching_rule.id,
            rationale=matching_rule.rationale,
        )

    def explain_action_name(self, action: str) -> PolicyExplanation:
        """Deny names outside the closed namespace without coercing them."""

        try:
            resolved_action = PolicyAction(action)
        except ValueError:
            return PolicyExplanation(
                action=action,
                result=PolicyResult.DENY,
                rule_id="unknown_action",
                rationale="Unknown action is denied by the closed policy namespace.",
                normalized_inputs=None,
            )
        decision = self.evaluate(ApprovalSchema(action=resolved_action))
        return PolicyExplanation(
            action=action,
            result=decision.result,
            rule_id=decision.policy_rule_id,
            rationale=decision.rationale,
            normalized_inputs=decision.normalized_inputs,
        )

    def _evaluate_human_required(self, schema: ApprovalSchema) -> PolicyDecision:
        if self._approval_registry is None:
            return self._decision(
                schema,
                result=PolicyResult.REQUIRE_HUMAN,
                rule_id="human_approval_required",
                rationale="The action requires a current interactive human approval.",
            )
        if (
            HardRuleEnforcer.requires_dual_human(schema)
            and len(self._approval_registry.registered_human_ids) < 2
        ):
            return self._decision(
                schema,
                result=PolicyResult.DENY,
                rule_id="dual_human_unavailable",
                rationale="The action requires two distinct registered human principals.",
            )
        approval = self._approval_registry.get_approval(schema)
        if approval is None:
            return self._decision(
                schema,
                result=PolicyResult.REQUIRE_HUMAN,
                rule_id="human_approval_required",
                rationale="The action requires a current interactive human approval.",
            )
        try:
            HardRuleEnforcer.enforce_approval(
                schema,
                approval.approvers,
                self._approval_registry.registered_human_ids,
            )
        except HardRuleError as error:
            return self._decision(
                schema,
                result=PolicyResult.DENY,
                rule_id="hard_rule",
                rationale=str(error),
            )
        return self._decision(
            schema,
            result=PolicyResult.ALLOW,
            rule_id="human_approval",
            rationale="The current action digest has an independent human approval.",
        )

    def _decision(
        self,
        schema: ApprovalSchema,
        *,
        result: PolicyResult,
        rule_id: str,
        rationale: str,
    ) -> PolicyDecision:
        return PolicyDecision(
            id=PolicyDecisionId(str(uuid4())),
            action=schema.action,
            normalized_inputs=schema.canonical_inputs(),
            policy_rule_id=rule_id,
            policy_version=self._policy_version,
            result=result,
            rationale=rationale,
            input_digest=schema.digest(),
            decided_at=datetime.now(UTC),
        )


__all__ = ["PolicyEvaluator", "PolicyExplanation", "PolicyRule"]
