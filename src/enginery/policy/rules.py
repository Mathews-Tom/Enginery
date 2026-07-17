"""Closed, non-overridable policy constraints."""

from __future__ import annotations

from collections.abc import Collection

from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.schemas import ActionSchemaError, ApprovalSchema


class HardRuleError(ActionSchemaError):
    """Raised when a request attempts to violate non-overridable governance."""


_HUMAN_REQUIRED_ACTIONS = frozenset(
    {
        PolicyAction.POLICY_OVERRIDE,
        PolicyAction.EVIDENCE_NON_APPLICABILITY_ACCEPT,
        PolicyAction.REVIEW_FINDING_WAIVE,
        PolicyAction.FACTORY_CHANGE_CANARY,
        PolicyAction.FACTORY_CHANGE_PROMOTE,
    }
)
_DUAL_HUMAN_ACTIONS = frozenset(
    {PolicyAction.FACTORY_CHANGE_CANARY, PolicyAction.FACTORY_CHANGE_PROMOTE}
)
_FORBIDDEN_OVERRIDE_SCOPES = frozenset(
    {
        "action_namespace",
        "approval_digest",
        "credential_confinement",
        "evidence.hard_required",
        "evidence.current_subject",
        "factory_candidate_isolation",
        "held_out_secrecy",
        "producer_separation",
        "reconciliation_before_retry",
    }
)


class HardRuleEnforcer:
    """Enforce constraints before rule matching or any authority grant."""

    @staticmethod
    def requires_human(schema: ApprovalSchema) -> bool:
        return schema.action in _HUMAN_REQUIRED_ACTIONS or (
            schema.action is PolicyAction.CAPABILITY_MATERIALIZE
            and schema.capability_introduced_by_run is True
        )

    @staticmethod
    def requires_dual_human(schema: ApprovalSchema) -> bool:
        """Return whether the action needs two registered human principals."""

        return schema.action in _DUAL_HUMAN_ACTIONS

    @staticmethod
    def _require(schema: ApprovalSchema, *fields: str) -> None:
        try:
            schema.require_fields(*fields)
        except ActionSchemaError as error:
            raise HardRuleError(str(error)) from error

    @staticmethod
    def enforce_request(schema: ApprovalSchema) -> None:
        """Reject unsafe input before policy rules can consider it."""

        if schema.action is PolicyAction.CREDENTIAL_GRANT:
            HardRuleEnforcer._require(
                schema,
                "credential_delivery_target",
                "agent_authored_executable",
            )
            if schema.credential_delivery_target != "fixed_broker":
                raise HardRuleError(
                    "production and publication credentials may only be granted "
                    "to fixed broker code"
                )
            if schema.agent_authored_executable is True:
                raise HardRuleError("broker credentials cannot execute agent-authored executables")
        if (
            schema.retry_after_ambiguous_effect is True
            and schema.reconciliation_complete is not True
        ):
            raise HardRuleError("an ambiguous side effect must reconcile before retry")
        if schema.action is PolicyAction.FACTORY_CHANGE_PROPOSE:
            HardRuleEnforcer._require(
                schema,
                "mutates_active_factory_asset",
                "candidate_received_held_out_input",
            )
        if schema.mutates_active_factory_asset is True:
            raise HardRuleError("active factory assets cannot be mutated in place")
        if schema.candidate_received_held_out_input is True:
            raise HardRuleError("candidates cannot inspect held-out evaluation inputs")
        if schema.action is PolicyAction.FACTORY_CHANGE_CANARY:
            HardRuleEnforcer._require(
                schema,
                "candidate_affects_protected_control",
                "canary_target",
            )
            if (
                schema.candidate_affects_protected_control is True
                and schema.canary_target != "non_production_shadow"
            ):
                raise HardRuleError(
                    "protected-control candidates may only canary in non-production shadow mode"
                )
        if schema.action is PolicyAction.POLICY_OVERRIDE:
            HardRuleEnforcer._require(
                schema,
                "override_reason",
                "override_scope",
                "override_expires_at",
            )
            if _FORBIDDEN_OVERRIDE_SCOPES.intersection(schema.override_scope or ()):
                raise HardRuleError("a policy override cannot relax a hard rule")
        if (
            schema.action is PolicyAction.CAPABILITY_MATERIALIZE
            and schema.capability_introduced_by_run is True
        ):
            HardRuleEnforcer._require(
                schema,
                "requested_capability",
                "diff_or_artifact_digest",
            )

    @staticmethod
    def enforce_approval(
        schema: ApprovalSchema,
        approvers: Collection[AuthorityPrincipal],
        registered_human_ids: Collection[str],
    ) -> None:
        """Apply producer and dual-human separation to an active approval."""

        if not approvers:
            raise HardRuleError("an authority decision requires at least one approver")
        producer_ids = set(schema.producer_principal_ids or ())
        if schema.requesting_principal_id is not None:
            producer_ids.add(schema.requesting_principal_id)
        for approver in approvers:
            if approver.id in producer_ids:
                raise HardRuleError(
                    "producer separation violated: an actor cannot approve "
                    "its own request or output"
                )
            if HardRuleEnforcer.requires_human(schema):
                if approver.principal_type is not PrincipalType.HUMAN:
                    raise HardRuleError("this action requires an interactive human approval")
                if approver.id not in registered_human_ids:
                    raise HardRuleError("the approving human principal is not registered")
        if schema.action in _DUAL_HUMAN_ACTIONS:
            approver_ids = {approver.id for approver in approvers}
            if len(set(registered_human_ids)) < 2:
                raise HardRuleError(
                    "dual-human separation is unavailable with fewer than two registered humans"
                )
            if len(approver_ids) < 2:
                raise HardRuleError("dual-human separation requires two distinct human approvers")
            if any(approver_id not in registered_human_ids for approver_id in approver_ids):
                raise HardRuleError("dual-human approvers must be registered human principals")


__all__ = ["HardRuleEnforcer", "HardRuleError"]
