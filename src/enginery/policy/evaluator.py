"""Closed policy evaluation with explicit default denial."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from enginery.domain.enums import RiskClass
from enginery.domain.ids import PolicyDecisionId
from enginery.domain.policy_decision import PolicyAction, PolicyDecision, PolicyResult
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
    """Evaluate only registered, exact action rules; all other requests deny."""

    def __init__(self, policy_version: str, rules: Iterable[PolicyRule] = ()) -> None:
        if not policy_version.strip():
            raise ValueError("policy_version must be non-blank")
        self._policy_version = policy_version
        self._rules = tuple(rules)

    def evaluate(self, schema: ApprovalSchema) -> PolicyDecision:
        """Return the first exact matching rule or a durable default denial."""

        matching_rule = next((rule for rule in self._rules if rule.matches(schema)), None)
        if matching_rule is None:
            result = PolicyResult.DENY
            rule_id = "default_deny"
            rationale = "Default deny: no matching policy rule found."
        else:
            result = matching_rule.result
            rule_id = matching_rule.id
            rationale = matching_rule.rationale
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


__all__ = ["PolicyEvaluator", "PolicyExplanation", "PolicyRule"]
