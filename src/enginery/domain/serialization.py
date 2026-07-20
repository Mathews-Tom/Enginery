"""Versioned JSON serialization for domain aggregates: event payloads and
schemas are versioned.

Every aggregate has exactly one currently supported ``schema_version`` per
envelope in this milestone. ``from_dict`` rejects any other version rather
than guessing at a migration — a version bump requires a new golden fixture
and an explicit migration path in a later milestone. Each ``to_dict``/
``from_dict`` pair round-trips through plain JSON-serializable primitives
(``str``, ``int``, ``float``, ``bool``, ``None``, ``list``, ``dict``) so a
fixture can be committed as ordinary JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from enginery.domain.artifact import Artifact, ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import FailureClass, InvalidInputError
from enginery.domain.factory_change import FactoryChange, FactoryChangeState
from enginery.domain.ids import (
    ArtifactId,
    FactoryChangeId,
    InterventionId,
    NodeAttemptId,
    NodeId,
    OutcomeId,
    PolicyDecisionId,
    RunId,
    WorkflowDefinitionId,
    WorkItemId,
)
from enginery.domain.intervention import Intervention, InterventionKind
from enginery.domain.node_attempt import (
    EvidenceResult,
    NodeAttempt,
    NodeAttemptState,
    ReconciliationResult,
)
from enginery.domain.outcome import Outcome, OutcomeKind
from enginery.domain.plan_execution import PlanExecution
from enginery.domain.policy_decision import PolicyAction, PolicyDecision, PolicyResult
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.manifest import WorkflowManifest

WORK_ITEM_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 2
NODE_ATTEMPT_SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
INTERVENTION_SCHEMA_VERSION = 1
OUTCOME_SCHEMA_VERSION = 1
FACTORY_CHANGE_SCHEMA_VERSION = 1
WORKFLOW_MANIFEST_SCHEMA_VERSION = 1
PLAN_EXECUTION_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# shared primitive codecs
# ---------------------------------------------------------------------------


def _encode_datetime(value: datetime) -> str:
    return value.isoformat()


def _decode_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidInputError(f"{field_name} must be a string", details={"field": field_name})
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise InvalidInputError(
            f"{field_name} must include a timezone offset", details={"field": field_name}
        )
    return parsed


def _decode_optional_datetime(value: object, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _decode_datetime(value, field_name=field_name)


def _encode_digest(value: Digest) -> str:
    return str(value)


def _decode_digest(value: object, *, field_name: str) -> Digest:
    if not isinstance(value, str) or ":" not in value:
        raise InvalidInputError(
            f"{field_name} must be a 'algorithm:hex' digest string", details={"field": field_name}
        )
    algorithm, _, hex_value = value.partition(":")
    return Digest(algorithm=algorithm, hex_value=hex_value)


def _envelope(schema_version: int, data: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": schema_version, "data": dict(data)}


def _unwrap_envelope(
    raw: Mapping[str, object], *, expected_schema_version: int, type_name: str
) -> Mapping[str, object]:
    schema_version = raw.get("schema_version")
    if schema_version != expected_schema_version:
        raise InvalidInputError(
            f"unsupported {type_name} schema_version",
            details={"expected": expected_schema_version, "actual": schema_version},
        )
    data = raw.get("data")
    if not isinstance(data, Mapping):
        raise InvalidInputError(f"{type_name} envelope is missing 'data'")
    return data


# ---------------------------------------------------------------------------
# WorkItem
# ---------------------------------------------------------------------------


def work_item_to_dict(item: WorkItem) -> dict[str, object]:
    return _envelope(
        WORK_ITEM_SCHEMA_VERSION,
        {
            "id": str(item.id),
            "work_kind": item.work_kind.value,
            "source_provider": item.source_provider,
            "external_reference": item.external_reference,
            "source_snapshot_reference": item.source_snapshot_reference,
            "title": item.title,
            "objective": item.objective,
            "acceptance_criteria": list(item.acceptance_criteria),
            "constraints": list(item.constraints),
            "risk_class": item.risk_class.value,
            "repository_targets": list(item.repository_targets),
            "dependencies": [str(dependency) for dependency in item.dependencies],
            "state": item.state.value,
            "aggregate_version": item.aggregate_version,
        },
    )


def work_item_from_dict(raw: Mapping[str, object]) -> WorkItem:
    data = _unwrap_envelope(
        raw, expected_schema_version=WORK_ITEM_SCHEMA_VERSION, type_name="WorkItem"
    )
    return WorkItem(
        id=WorkItemId(_str(data, "id")),
        work_kind=WorkKind(_str(data, "work_kind")),
        source_provider=_str(data, "source_provider"),
        external_reference=_str(data, "external_reference"),
        source_snapshot_reference=_str(data, "source_snapshot_reference"),
        title=_str(data, "title"),
        objective=_str(data, "objective"),
        acceptance_criteria=tuple(_str_list(data, "acceptance_criteria")),
        constraints=tuple(_str_list(data, "constraints")),
        risk_class=RiskClass(_str(data, "risk_class")),
        repository_targets=tuple(_str_list(data, "repository_targets")),
        dependencies=tuple(WorkItemId(value) for value in _str_list(data, "dependencies")),
        state=WorkItemState(_str(data, "state")),
        aggregate_version=_int(data, "aggregate_version"),
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_to_dict(run: Run) -> dict[str, object]:
    if set(run.adapter_versions) != set(run.adapter_fingerprints):
        raise InvalidInputError(
            "Run adapter_versions and adapter_fingerprints must name the same adapters"
        )
    return _envelope(
        RUN_SCHEMA_VERSION,
        {
            "id": str(run.id),
            "work_item_id": str(run.work_item_id),
            "work_item_snapshot_digest": _encode_digest(run.work_item_snapshot_digest),
            "workflow_definition_id": str(run.workflow_definition_id),
            "workflow_definition_digest": _encode_digest(run.workflow_definition_digest),
            "repository": run.repository,
            "base_revision": run.base_revision,
            "policy_set_version": run.policy_set_version,
            "adapter_versions": dict(run.adapter_versions),
            "adapter_fingerprints": {
                adapter: _encode_digest(fingerprint)
                for adapter, fingerprint in run.adapter_fingerprints.items()
            },
            "capability_lock_digest": _encode_digest(run.capability_lock_digest),
            "environment_manifest_digest": _encode_digest(run.environment_manifest_digest),
            "configuration_snapshot_digest": _encode_digest(run.configuration_snapshot_digest),
            "state": run.state.value,
            "aggregate_version": run.aggregate_version,
        },
    )


def run_from_dict(raw: Mapping[str, object]) -> Run:
    schema_version = raw.get("schema_version")
    if schema_version not in {1, RUN_SCHEMA_VERSION}:
        raise InvalidInputError(
            "unsupported Run schema_version",
            details={"expected": RUN_SCHEMA_VERSION, "actual": schema_version},
        )
    data = raw.get("data")
    if not isinstance(data, Mapping):
        raise InvalidInputError("Run envelope is missing 'data'")
    adapter_versions_raw = data.get("adapter_versions")
    if not isinstance(adapter_versions_raw, Mapping):
        raise InvalidInputError("adapter_versions must be a mapping")
    adapter_fingerprints_raw = data.get("adapter_fingerprints", {})
    if not isinstance(adapter_fingerprints_raw, Mapping):
        raise InvalidInputError("adapter_fingerprints must be a mapping")
    return Run(
        id=RunId(_str(data, "id")),
        work_item_id=WorkItemId(_str(data, "work_item_id")),
        work_item_snapshot_digest=_decode_digest(
            data.get("work_item_snapshot_digest"), field_name="work_item_snapshot_digest"
        ),
        workflow_definition_id=WorkflowDefinitionId(_str(data, "workflow_definition_id")),
        workflow_definition_digest=_decode_digest(
            data.get("workflow_definition_digest"), field_name="workflow_definition_digest"
        ),
        repository=_str(data, "repository"),
        base_revision=_str(data, "base_revision"),
        policy_set_version=_str(data, "policy_set_version"),
        adapter_versions={str(key): str(value) for key, value in adapter_versions_raw.items()},
        adapter_fingerprints={
            str(adapter): _decode_digest(fingerprint, field_name="adapter_fingerprints")
            for adapter, fingerprint in adapter_fingerprints_raw.items()
        },
        capability_lock_digest=_decode_digest(
            data.get("capability_lock_digest"), field_name="capability_lock_digest"
        ),
        environment_manifest_digest=_decode_digest(
            data.get("environment_manifest_digest"), field_name="environment_manifest_digest"
        ),
        configuration_snapshot_digest=_decode_digest(
            data.get("configuration_snapshot_digest"), field_name="configuration_snapshot_digest"
        ),
        state=RunState(_str(data, "state")),
        aggregate_version=_int(data, "aggregate_version"),
    )


# ---------------------------------------------------------------------------
# NodeAttempt
# ---------------------------------------------------------------------------


def node_attempt_to_dict(attempt: NodeAttempt) -> dict[str, object]:
    return _envelope(
        NODE_ATTEMPT_SCHEMA_VERSION,
        {
            "id": str(attempt.id),
            "run_id": str(attempt.run_id),
            "node_id": str(attempt.node_id),
            "attempt_number": attempt.attempt_number,
            "actor": attempt.actor,
            "input_digest": _encode_digest(attempt.input_digest),
            "state": attempt.state.value,
            "lease_owner": attempt.lease_owner,
            "lease_expires_at": _optional_datetime_out(attempt.lease_expires_at),
            "started_at": _optional_datetime_out(attempt.started_at),
            "completed_at": _optional_datetime_out(attempt.completed_at),
            "emitted_event_range": list(attempt.emitted_event_range)
            if attempt.emitted_event_range is not None
            else None,
            "output_artifact_ids": [str(item) for item in attempt.output_artifact_ids],
            "evidence_result": attempt.evidence_result.value
            if attempt.evidence_result is not None
            else None,
            "cost_amount": attempt.cost_amount,
            "duration_seconds": attempt.duration_seconds,
            "failure_class": attempt.failure_class.value
            if attempt.failure_class is not None
            else None,
            "reconciliation_result": attempt.reconciliation_result.value
            if attempt.reconciliation_result is not None
            else None,
            "schema_version": attempt.schema_version,
        },
    )


def node_attempt_from_dict(raw: Mapping[str, object]) -> NodeAttempt:
    data = _unwrap_envelope(
        raw, expected_schema_version=NODE_ATTEMPT_SCHEMA_VERSION, type_name="NodeAttempt"
    )
    event_range_raw = data.get("emitted_event_range")
    event_range: tuple[int, int] | None = None
    if event_range_raw is not None:
        if not isinstance(event_range_raw, list) or len(event_range_raw) != 2:
            raise InvalidInputError("emitted_event_range must be a two-element list")
        event_range = (int(event_range_raw[0]), int(event_range_raw[1]))
    evidence_result_raw = data.get("evidence_result")
    failure_class_raw = data.get("failure_class")
    reconciliation_result_raw = data.get("reconciliation_result")
    return NodeAttempt(
        id=NodeAttemptId(_str(data, "id")),
        run_id=RunId(_str(data, "run_id")),
        node_id=NodeId(_str(data, "node_id")),
        attempt_number=_int(data, "attempt_number"),
        actor=_str(data, "actor"),
        input_digest=_decode_digest(data.get("input_digest"), field_name="input_digest"),
        state=NodeAttemptState(_str(data, "state")),
        lease_owner=_optional_str(data, "lease_owner"),
        lease_expires_at=_decode_optional_datetime(
            data.get("lease_expires_at"), field_name="lease_expires_at"
        ),
        started_at=_decode_optional_datetime(data.get("started_at"), field_name="started_at"),
        completed_at=_decode_optional_datetime(data.get("completed_at"), field_name="completed_at"),
        emitted_event_range=event_range,
        output_artifact_ids=tuple(
            ArtifactId(value) for value in _str_list(data, "output_artifact_ids")
        ),
        evidence_result=EvidenceResult(evidence_result_raw)
        if isinstance(evidence_result_raw, str)
        else None,
        cost_amount=_optional_float(data, "cost_amount"),
        duration_seconds=_optional_float(data, "duration_seconds"),
        failure_class=FailureClass(failure_class_raw)
        if isinstance(failure_class_raw, str)
        else None,
        reconciliation_result=ReconciliationResult(reconciliation_result_raw)
        if isinstance(reconciliation_result_raw, str)
        else None,
        schema_version=_int(data, "schema_version"),
    )


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def artifact_to_dict(artifact: Artifact) -> dict[str, object]:
    return _envelope(
        ARTIFACT_SCHEMA_VERSION,
        {
            "id": str(artifact.id),
            "digest": _encode_digest(artifact.digest),
            "byte_size": artifact.byte_size,
            "media_type": artifact.media_type,
            "kind": artifact.kind.value,
            "run_id": str(artifact.run_id),
            "node_id": str(artifact.node_id),
            "attempt_id": str(artifact.attempt_id),
            "storage_reference": artifact.storage_reference,
            "redaction": artifact.redaction.value,
            "created_at": _encode_datetime(artifact.created_at),
            "schema_version": artifact.schema_version,
        },
    )


def artifact_from_dict(raw: Mapping[str, object]) -> Artifact:
    data = _unwrap_envelope(
        raw, expected_schema_version=ARTIFACT_SCHEMA_VERSION, type_name="Artifact"
    )
    return Artifact(
        id=ArtifactId(_str(data, "id")),
        digest=_decode_digest(data.get("digest"), field_name="digest"),
        byte_size=_int(data, "byte_size"),
        media_type=_str(data, "media_type"),
        kind=ArtifactKind(_str(data, "kind")),
        run_id=RunId(_str(data, "run_id")),
        node_id=NodeId(_str(data, "node_id")),
        attempt_id=NodeAttemptId(_str(data, "attempt_id")),
        storage_reference=_str(data, "storage_reference"),
        redaction=RedactionClassification(_str(data, "redaction")),
        created_at=_decode_datetime(data.get("created_at"), field_name="created_at"),
        schema_version=_int(data, "schema_version"),
    )


# ---------------------------------------------------------------------------
# PolicyDecision
# ---------------------------------------------------------------------------


def policy_decision_to_dict(decision: PolicyDecision) -> dict[str, object]:
    return _envelope(
        POLICY_DECISION_SCHEMA_VERSION,
        {
            "id": str(decision.id),
            "action": decision.action.value,
            "normalized_inputs": dict(decision.normalized_inputs),
            "policy_rule_id": decision.policy_rule_id,
            "policy_version": decision.policy_version,
            "result": decision.result.value,
            "rationale": decision.rationale,
            "input_digest": _encode_digest(decision.input_digest),
            "decided_at": _encode_datetime(decision.decided_at),
            "required_evidence": list(decision.required_evidence),
            "required_approver": decision.required_approver,
            "superseded": decision.superseded,
            "superseded_by": str(decision.superseded_by)
            if decision.superseded_by is not None
            else None,
        },
    )


def policy_decision_from_dict(raw: Mapping[str, object]) -> PolicyDecision:
    data = _unwrap_envelope(
        raw, expected_schema_version=POLICY_DECISION_SCHEMA_VERSION, type_name="PolicyDecision"
    )
    normalized_inputs_raw = data.get("normalized_inputs")
    if not isinstance(normalized_inputs_raw, Mapping):
        raise InvalidInputError("normalized_inputs must be a mapping")
    superseded_by_raw = data.get("superseded_by")
    return PolicyDecision(
        id=PolicyDecisionId(_str(data, "id")),
        action=PolicyAction(_str(data, "action")),
        normalized_inputs=dict(normalized_inputs_raw),
        policy_rule_id=_str(data, "policy_rule_id"),
        policy_version=_str(data, "policy_version"),
        result=PolicyResult(_str(data, "result")),
        rationale=_str(data, "rationale"),
        input_digest=_decode_digest(data.get("input_digest"), field_name="input_digest"),
        decided_at=_decode_datetime(data.get("decided_at"), field_name="decided_at"),
        required_evidence=tuple(_str_list(data, "required_evidence")),
        required_approver=_optional_str(data, "required_approver"),
        superseded=bool(data.get("superseded", False)),
        superseded_by=PolicyDecisionId(superseded_by_raw)
        if isinstance(superseded_by_raw, str)
        else None,
    )


# ---------------------------------------------------------------------------
# Intervention
# ---------------------------------------------------------------------------


def intervention_to_dict(intervention: Intervention) -> dict[str, object]:
    return _envelope(
        INTERVENTION_SCHEMA_VERSION,
        {
            "id": str(intervention.id),
            "kind": intervention.kind.value,
            "run_id": str(intervention.run_id),
            "actor": intervention.actor,
            "occurred_at": _encode_datetime(intervention.occurred_at),
            "rationale": intervention.rationale,
            "detail": dict(intervention.detail),
        },
    )


def intervention_from_dict(raw: Mapping[str, object]) -> Intervention:
    data = _unwrap_envelope(
        raw, expected_schema_version=INTERVENTION_SCHEMA_VERSION, type_name="Intervention"
    )
    detail_raw = data.get("detail", {})
    if not isinstance(detail_raw, Mapping):
        raise InvalidInputError("detail must be a mapping")
    return Intervention(
        id=InterventionId(_str(data, "id")),
        kind=InterventionKind(_str(data, "kind")),
        run_id=RunId(_str(data, "run_id")),
        actor=_str(data, "actor"),
        occurred_at=_decode_datetime(data.get("occurred_at"), field_name="occurred_at"),
        rationale=_str(data, "rationale"),
        detail=dict(detail_raw),
    )


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


def outcome_to_dict(outcome: Outcome) -> dict[str, object]:
    return _envelope(
        OUTCOME_SCHEMA_VERSION,
        {
            "id": str(outcome.id),
            "work_item_id": str(outcome.work_item_id),
            "kind": outcome.kind.value,
            "observed_at": _encode_datetime(outcome.observed_at),
            "run_id": str(outcome.run_id) if outcome.run_id is not None else None,
            "linked_work_item_id": str(outcome.linked_work_item_id)
            if outcome.linked_work_item_id is not None
            else None,
            "detail": dict(outcome.detail),
            "schema_version": outcome.schema_version,
        },
    )


def outcome_from_dict(raw: Mapping[str, object]) -> Outcome:
    data = _unwrap_envelope(
        raw, expected_schema_version=OUTCOME_SCHEMA_VERSION, type_name="Outcome"
    )
    detail_raw = data.get("detail", {})
    if not isinstance(detail_raw, Mapping):
        raise InvalidInputError("detail must be a mapping")
    run_id_raw = data.get("run_id")
    linked_raw = data.get("linked_work_item_id")
    return Outcome(
        id=OutcomeId(_str(data, "id")),
        work_item_id=WorkItemId(_str(data, "work_item_id")),
        kind=OutcomeKind(_str(data, "kind")),
        observed_at=_decode_datetime(data.get("observed_at"), field_name="observed_at"),
        run_id=RunId(run_id_raw) if isinstance(run_id_raw, str) else None,
        linked_work_item_id=WorkItemId(linked_raw) if isinstance(linked_raw, str) else None,
        detail=dict(detail_raw),
        schema_version=_int(data, "schema_version"),
    )


# ---------------------------------------------------------------------------
# FactoryChange
# ---------------------------------------------------------------------------


def factory_change_to_dict(change: FactoryChange) -> dict[str, object]:
    return _envelope(
        FACTORY_CHANGE_SCHEMA_VERSION,
        {
            "id": str(change.id),
            "affected_asset": change.affected_asset,
            "baseline_version": change.baseline_version,
            "problem_statement": change.problem_statement,
            "hypothesis": change.hypothesis,
            "candidate_version": change.candidate_version,
            "state": change.state.value,
            "evaluation_set_digest": change.evaluation_set_digest,
            "comparison_result": dict(change.comparison_result)
            if change.comparison_result is not None
            else None,
            "approval_state": change.approval_state,
            "canary_cohort": list(change.canary_cohort),
            "promotion_result": change.promotion_result,
            "aggregate_version": change.aggregate_version,
        },
    )


def factory_change_from_dict(raw: Mapping[str, object]) -> FactoryChange:
    data = _unwrap_envelope(
        raw, expected_schema_version=FACTORY_CHANGE_SCHEMA_VERSION, type_name="FactoryChange"
    )
    comparison_result_raw = data.get("comparison_result")
    if comparison_result_raw is not None and not isinstance(comparison_result_raw, Mapping):
        raise InvalidInputError("comparison_result must be a mapping or null")
    return FactoryChange(
        id=FactoryChangeId(_str(data, "id")),
        affected_asset=_str(data, "affected_asset"),
        baseline_version=_str(data, "baseline_version"),
        problem_statement=_str(data, "problem_statement"),
        hypothesis=_str(data, "hypothesis"),
        candidate_version=_str(data, "candidate_version"),
        state=FactoryChangeState(_str(data, "state")),
        evaluation_set_digest=_optional_str(data, "evaluation_set_digest"),
        comparison_result=dict(comparison_result_raw)
        if comparison_result_raw is not None
        else None,
        approval_state=_optional_str(data, "approval_state"),
        canary_cohort=tuple(_str_list(data, "canary_cohort")),
        promotion_result=_optional_str(data, "promotion_result"),
        aggregate_version=_int(data, "aggregate_version"),
    )


# ---------------------------------------------------------------------------
# WorkflowManifest
# ---------------------------------------------------------------------------


def workflow_manifest_to_dict(manifest: WorkflowManifest) -> dict[str, object]:
    return _envelope(WORKFLOW_MANIFEST_SCHEMA_VERSION, manifest.to_mapping())


def workflow_manifest_from_dict(raw: Mapping[str, object]) -> WorkflowManifest:
    data = _unwrap_envelope(
        raw,
        expected_schema_version=WORKFLOW_MANIFEST_SCHEMA_VERSION,
        type_name="WorkflowManifest",
    )
    return WorkflowManifest.from_mapping(data)


# ---------------------------------------------------------------------------
# PlanExecution
# ---------------------------------------------------------------------------


def plan_execution_to_dict(plan_execution: PlanExecution) -> dict[str, object]:
    return _envelope(PLAN_EXECUTION_SCHEMA_VERSION, plan_execution.to_mapping())


def plan_execution_from_dict(raw: Mapping[str, object]) -> PlanExecution:
    data = _unwrap_envelope(
        raw, expected_schema_version=PLAN_EXECUTION_SCHEMA_VERSION, type_name="PlanExecution"
    )
    return PlanExecution.from_mapping(data)


# ---------------------------------------------------------------------------
# field-level helpers
# ---------------------------------------------------------------------------


def _str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise InvalidInputError(f"{key!r} must be a string", details={"field": key})
    return value


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInputError(f"{key!r} must be a string or null", details={"field": key})
    return value


def _int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidInputError(f"{key!r} must be an integer", details={"field": key})
    return value


def _optional_float(data: Mapping[str, object], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidInputError(f"{key!r} must be a number or null", details={"field": key})
    return float(value)


def _str_list(data: Mapping[str, object], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidInputError(f"{key!r} must be a list of strings", details={"field": key})
    return value


def _optional_datetime_out(value: datetime | None) -> str | None:
    return _encode_datetime(value) if value is not None else None


__all__: list[str] = [
    "ARTIFACT_SCHEMA_VERSION",
    "FACTORY_CHANGE_SCHEMA_VERSION",
    "INTERVENTION_SCHEMA_VERSION",
    "NODE_ATTEMPT_SCHEMA_VERSION",
    "OUTCOME_SCHEMA_VERSION",
    "PLAN_EXECUTION_SCHEMA_VERSION",
    "POLICY_DECISION_SCHEMA_VERSION",
    "RUN_SCHEMA_VERSION",
    "WORKFLOW_MANIFEST_SCHEMA_VERSION",
    "WORK_ITEM_SCHEMA_VERSION",
    "artifact_from_dict",
    "artifact_to_dict",
    "factory_change_from_dict",
    "factory_change_to_dict",
    "intervention_from_dict",
    "intervention_to_dict",
    "node_attempt_from_dict",
    "node_attempt_to_dict",
    "outcome_from_dict",
    "outcome_to_dict",
    "plan_execution_from_dict",
    "plan_execution_to_dict",
    "policy_decision_from_dict",
    "policy_decision_to_dict",
    "run_from_dict",
    "run_to_dict",
    "work_item_from_dict",
    "work_item_to_dict",
    "workflow_manifest_from_dict",
    "workflow_manifest_to_dict",
]
