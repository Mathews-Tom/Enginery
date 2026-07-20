"""Default policy rule tables for closed actions the policy layer does not itself decide.

``PolicyEvaluator`` default-denies any action with no matching
``PolicyRule``; the closed hard-rule layer in ``rules.py`` enforces
cross-cutting invariants but never supplies a rule table. Each concrete
workflow supplies the rules that apply to it. This module is the single,
reviewable source for the Stage 2 (plan-to-release) rule set so no two
call sites define divergent policy for the same action.
"""

from __future__ import annotations

from enginery.domain.enums import RiskClass
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.policy.evaluator import PolicyRule

_LOW_AND_MEDIUM_RISK = frozenset({RiskClass.LOW, RiskClass.MEDIUM})


def stage2_policy_rules() -> tuple[PolicyRule, ...]:
    """The reviewed rule set for merge and release actions.

    ``pull_request.merge`` is allowed for low/medium risk work once the
    merge-execution service's own evidence check (fresh CI, current head,
    no conflicts) has already passed -- the policy layer is confirming
    permission, not re-deriving evidence. High risk work is not covered
    by any rule here and default-denies, requiring an explicit override.

    ``release.prepare`` is allowed once constituent work is merged; the
    release-manifest broker (not this rule) is the component that
    actually verifies constituent-merge state before calling ``publish``,
    matching design's "version/changelog preparation cannot begin before
    implementation gates pass."

    ``release.publish`` is deliberately absent from this table.
    Publication requires a current, interactive human approval for every
    invocation -- see ``policy/rules.py``'s ``_HUMAN_REQUIRED_ACTIONS``,
    which routes ``release.publish`` through the same approval-registry
    machinery already built and tested for ``policy.override`` and
    similar interactive-only actions, rather than a plain allow/deny row.
    """
    return (
        PolicyRule(
            id="stage2_allow_merge_low_medium_risk",
            action=PolicyAction.PULL_REQUEST_MERGE,
            result=PolicyResult.ALLOW,
            rationale=(
                "Low and medium risk merges are permitted once fresh, "
                "current-head evidence has passed the merge-ready contract."
            ),
            risk_classes=_LOW_AND_MEDIUM_RISK,
        ),
        PolicyRule(
            id="stage2_allow_release_prepare_low_medium_risk",
            action=PolicyAction.RELEASE_PREPARE,
            result=PolicyResult.ALLOW,
            rationale=(
                "Release preparation is permitted for low/medium risk work; "
                "the release-manifest broker itself refuses to proceed "
                "until every constituent milestone is externally merged."
            ),
            risk_classes=_LOW_AND_MEDIUM_RISK,
        ),
    )


__all__ = ["stage2_policy_rules"]
