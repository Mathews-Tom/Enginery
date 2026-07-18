"""Canonical, closed policy-action request schemas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.policy_decision import PolicyAction


class ActionSchemaError(InvalidInputError):
    """Raised when an action request is incomplete or not normalized."""


_ACTION_FIELDS = (
    "work_kind",
    "risk_class",
    "repository",
    "changed_paths",
    "requested_capability",
    "workflow_version",
    "policy_version",
    "evidence_status",
    "retry_consumption",
    "budget_consumption",
    "validation_profile",
    "release_type",
    "credential_request",
    "prior_interventions",
    "external_protection_state",
    "work_snapshot",
    "effective_configuration",
    "adapter_locks",
    "capability_locks",
    "target_resource",
    "diff_or_artifact_digest",
    "acceptance_criteria_snapshot",
    "evidence_bundle_digest",
    "requesting_principal_id",
    "producer_principal_ids",
    "credential_delivery_target",
    "agent_authored_executable",
    "retry_after_ambiguous_effect",
    "reconciliation_complete",
    "mutates_active_factory_asset",
    "candidate_received_held_out_input",
    "candidate_affects_protected_control",
    "canary_target",
    "override_reason",
    "override_scope",
    "override_expires_at",
    "capability_introduced_by_run",
)


@dataclass(frozen=True, slots=True)
class ApprovalSchema:
    """The complete, canonical policy input for one closed action."""

    action: PolicyAction
    work_kind: WorkKind | None = None
    risk_class: RiskClass | None = None
    repository: str | None = None
    changed_paths: tuple[str, ...] | None = None
    requested_capability: str | None = None
    workflow_version: str | None = None
    policy_version: str | None = None
    evidence_status: str | None = None
    retry_consumption: int | None = None
    budget_consumption: float | None = None
    validation_profile: str | None = None
    release_type: str | None = None
    credential_request: str | None = None
    prior_interventions: tuple[str, ...] | None = None
    external_protection_state: str | None = None
    work_snapshot: str | None = None
    effective_configuration: str | None = None
    adapter_locks: str | None = None
    capability_locks: str | None = None
    target_resource: str | None = None
    diff_or_artifact_digest: str | None = None
    acceptance_criteria_snapshot: str | None = None
    evidence_bundle_digest: str | None = None
    requesting_principal_id: str | None = None
    producer_principal_ids: tuple[str, ...] | None = None
    credential_delivery_target: str | None = None
    agent_authored_executable: bool | None = None
    retry_after_ambiguous_effect: bool | None = None
    reconciliation_complete: bool | None = None
    mutates_active_factory_asset: bool | None = None
    candidate_received_held_out_input: bool | None = None
    candidate_affects_protected_control: bool | None = None
    canary_target: str | None = None
    override_reason: str | None = None
    override_scope: tuple[str, ...] | None = None
    override_expires_at: datetime | None = None
    capability_introduced_by_run: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, PolicyAction):
            raise ActionSchemaError("action must be a member of the closed policy action namespace")
        for name in (
            "changed_paths",
            "prior_interventions",
            "producer_principal_ids",
            "override_scope",
        ):
            values = getattr(self, name)
            if values is not None and any(not value.strip() for value in values):
                raise ActionSchemaError(f"{name} cannot contain blank values")
        if self.override_expires_at is not None and self.override_expires_at.tzinfo is None:
            raise ActionSchemaError("override_expires_at must be timezone-aware")

    def canonical_inputs(self) -> Mapping[str, object]:
        """Return every policy-relevant field, including explicit nulls."""

        return {
            "action": self.action.value,
            "schema_version": 1,
            "work_kind": self.work_kind.value if self.work_kind is not None else None,
            "risk_class": self.risk_class.value if self.risk_class is not None else None,
            "repository": self.repository,
            "changed_paths": sorted(self.changed_paths) if self.changed_paths is not None else None,
            "requested_capability": self.requested_capability,
            "workflow_version": self.workflow_version,
            "policy_version": self.policy_version,
            "evidence_status": self.evidence_status,
            "retry_consumption": self.retry_consumption,
            "budget_consumption": self.budget_consumption,
            "validation_profile": self.validation_profile,
            "release_type": self.release_type,
            "credential_request": self.credential_request,
            "prior_interventions": (
                sorted(self.prior_interventions) if self.prior_interventions is not None else None
            ),
            "external_protection_state": self.external_protection_state,
            "work_snapshot": self.work_snapshot,
            "effective_configuration": self.effective_configuration,
            "adapter_locks": self.adapter_locks,
            "capability_locks": self.capability_locks,
            "target_resource": self.target_resource,
            "diff_or_artifact_digest": self.diff_or_artifact_digest,
            "acceptance_criteria_snapshot": self.acceptance_criteria_snapshot,
            "evidence_bundle_digest": self.evidence_bundle_digest,
            "requesting_principal_id": self.requesting_principal_id,
            "producer_principal_ids": (
                sorted(self.producer_principal_ids)
                if self.producer_principal_ids is not None
                else None
            ),
            "credential_delivery_target": self.credential_delivery_target,
            "agent_authored_executable": self.agent_authored_executable,
            "retry_after_ambiguous_effect": self.retry_after_ambiguous_effect,
            "reconciliation_complete": self.reconciliation_complete,
            "mutates_active_factory_asset": self.mutates_active_factory_asset,
            "candidate_received_held_out_input": self.candidate_received_held_out_input,
            "candidate_affects_protected_control": self.candidate_affects_protected_control,
            "canary_target": self.canary_target,
            "override_reason": self.override_reason,
            "override_scope": (
                sorted(self.override_scope) if self.override_scope is not None else None
            ),
            "override_expires_at": (
                self.override_expires_at.isoformat()
                if self.override_expires_at is not None
                else None
            ),
            "capability_introduced_by_run": self.capability_introduced_by_run,
        }

    def digest(self) -> Digest:
        """Compute the canonical approval digest for this exact request."""

        return Digest.of_json(self.canonical_inputs())

    def require_fields(self, *field_names: str) -> None:
        """Fail closed when an action-specific mandatory field is absent."""

        missing = [
            field_name
            for field_name in field_names
            if field_name not in _ACTION_FIELDS or getattr(self, field_name) is None
        ]
        if missing:
            raise ActionSchemaError(
                "missing required action fields",
                details={"action": self.action.value, "fields": missing},
            )


__all__ = ["ActionSchemaError", "ApprovalSchema"]
