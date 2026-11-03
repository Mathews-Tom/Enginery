"""Provenance-backed, ledger-only inspection of one durable Stage 1 run.

This consumer neither progresses a run nor opens its workspace or artifact files.
A merge-readiness completion is not a merge, nor a claim of autonomous delivery.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_receipt import ExecutionReceipt
from enginery.domain.ids import NodeId, ObservationId, OperationId, RunId
from enginery.domain.serialization import (
    node_attempt_from_dict,
    observation_from_dict,
    observation_to_dict,
    outcome_from_dict,
    outcome_to_dict,
)
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.evaluation.queries import list_failures, list_interventions
from enginery.ledger.human_decisions import list_decisions
from enginery.ledger.service import LedgerService
from enginery.workflows.run_intent import Stage1RunRequest, stage1_request_from_state


@dataclass(frozen=True)
class _Event:
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    commit_seq: int
    event_type: str
    recorded_at: str
    state: dict[str, object]

    def evidence(self) -> dict[str, object]:
        return {
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "commit_seq": self.commit_seq,
            "event_type": self.event_type,
            "recorded_at": self.recorded_at,
        }


def _malformed(source: object, error: object) -> InvalidInputError:
    return InvalidInputError(
        f"malformed outcome summary evidence at {source}: {error}",
        details={"source": source, "reason": str(error)},
    )


def _timestamp(value: object, source: object) -> datetime:
    if not isinstance(value, str):
        raise _malformed(source, "timestamp must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise _malformed(source, "invalid ISO-8601 timestamp") from error
    if result.tzinfo is None:
        raise _malformed(source, "timestamp must be timezone-aware")
    return result


def _events(ledger: LedgerService, where: str, parameters: tuple[object, ...]) -> list[_Event]:
    rows = ledger.connection.execute(
        "SELECT aggregate_type, aggregate_id, aggregate_version, commit_seq, event_type, "
        f"recorded_at, payload FROM events WHERE {where} ORDER BY commit_seq",
        parameters,
    )
    result: list[_Event] = []
    for row in rows:
        source = {"commit_seq": row["commit_seq"], "aggregate_id": row["aggregate_id"]}
        try:
            state = json.loads(row["payload"])
            if not isinstance(state, dict):
                raise ValueError("event payload must be a mapping")
        except (ValueError, TypeError) as error:
            raise _malformed(source, error) from error
        _timestamp(row["recorded_at"], source)
        result.append(
            _Event(
                row["aggregate_type"],
                row["aggregate_id"],
                row["aggregate_version"],
                row["commit_seq"],
                row["event_type"],
                row["recorded_at"],
                state,
            )
        )
    return result


def _unknown(reason: str) -> dict[str, object]:
    return {"state": "unknown", "value": None, "reason": reason}


def _text(state: Mapping[str, object], key: str, source: object) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _malformed(source, f"{key} must be a non-blank string")
    return value


def _generation(attempt_id: object) -> int | None:
    if not isinstance(attempt_id, str):
        return None
    suffix = attempt_id.rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else None


def _required_nodes(request: Stage1RunRequest) -> set[str]:
    """Follow the declared merge-ready terminal's dependencies, not optional branches."""
    required: set[str] = set()
    pending = [
        str(node_id)
        for node_id, terminal in request.manifest.terminal_state_mapping.items()
        if terminal == "merge_ready"
    ]
    while pending:
        node_id = pending.pop()
        if node_id in required:
            continue
        required.add(node_id)
        node = request.manifest.nodes[NodeId(node_id)]
        pending.extend(str(dependency) for dependency in node.dependencies)
    return required


def _native_complete(request: Stage1RunRequest, nodes: Mapping[str, _Event]) -> bool:
    required = _required_nodes(request)
    if not required or any(
        node_id not in nodes or nodes[node_id].state.get("status") != "passed"
        for node_id in required
    ):
        return False
    implement = nodes.get("implement")
    publication = nodes.get("open_pr")
    verify = nodes.get("verify")
    if implement is None or publication is None or verify is None:
        return False
    generation = _generation(implement.state.get("attempt_id"))
    if generation is None or any(
        _generation(nodes[node_id].state.get("attempt_id")) != generation
        for node_id in required - {"qualify"}
    ):
        return False
    number = publication.state.get("pull_request_number")
    head = publication.state.get("head_revision")
    return (
        publication.event_type == "runtime_node.deterministic_completed"
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and isinstance(head, str)
        and bool(head.strip())
        and verify.event_type == "runtime_node.deterministic_completed"
        and verify.state.get("pull_request_outcome") == "merge_ready"
        and verify.state.get("head_revision") == head
        and isinstance(verify.state.get("evidence_digest"), str)
        and bool(verify.state["evidence_digest"])
    )


def _downstream(ledger: LedgerService, run_id: RunId) -> dict[str, object]:
    service = OutcomeCaptureService(ledger)
    observations: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    # Scope before decoding so another run's malformed observation cannot poison this report.
    rows = ledger.connection.execute(
        "SELECT aggregate_id FROM projections WHERE aggregate_type = 'observation' "
        "AND (substr(aggregate_id, 1, ?) = ? OR "
        "CASE WHEN json_valid(state_json) THEN json_extract(state_json, '$.data.run_id') = ? END)",
        (len(str(run_id)) + 1, f"{run_id}:", str(run_id)),
    )
    for row in rows:
        observation_id = row["aggregate_id"]
        source = {"aggregate_type": "observation", "aggregate_id": observation_id}
        try:
            observation = service.read_observation(ObservationId(observation_id))
            if observation is None or observation.run_id != run_id:
                raise ValueError("observation run identity does not match")
            events = _events(
                ledger, "aggregate_type = 'observation' AND aggregate_id = ?", (observation_id,)
            )
            for event in events:
                observation_from_dict(event.state)
            _timestamp(observation.opened_at.isoformat(), source)
            observations.append(
                {
                    **observation_to_dict(observation),
                    "evidence": [event.evidence() for event in events],
                }
            )
            if observation.outcome_id is not None:
                outcome = service.read_outcome(observation.outcome_id)
                if outcome is None:
                    raise ValueError("captured observation references a missing outcome")
                outcome_events = _events(
                    ledger,
                    "aggregate_type = 'outcome' AND aggregate_id = ?",
                    (str(outcome.id),),
                )
                for event in outcome_events:
                    outcome_from_dict(event.state)
                _timestamp(outcome.observed_at.isoformat(), source)
                if outcome.work_item_id != observation.work_item_id:
                    raise ValueError("outcome work-item identity differs from its observation")
                outcomes.append(
                    {
                        **outcome_to_dict(outcome),
                        "evidence": [event.evidence() for event in outcome_events],
                    }
                )
        except (InvalidInputError, ValueError, TypeError, KeyError) as error:
            raise _malformed(source, error) from error
    return {
        "observations": observations,
        "outcomes": outcomes,
        "coverage": "recorded_observations_only",
        "missing_observation_state": "unknown",
    }


def _assistance(ledger: LedgerService, run_id: RunId, history: list[_Event]) -> dict[str, object]:
    decisions: list[dict[str, object]] = []
    decision_ids: set[str] = set()
    try:
        for decision in list_decisions(ledger.connection, str(run_id)):
            decision_ids.add(decision.id)
            decisions.append(
                {
                    "decision_id": decision.id,
                    "action": decision.action,
                    "reason": decision.reason,
                    "principal_id": decision.principal.id,
                    "decided_at": decision.decided_at.isoformat(),
                    "superseded_by": decision.superseded_by,
                    "evidence": {"table": "human_decisions", "decision_id": decision.id},
                }
            )
    except (InvalidInputError, ValueError, TypeError, KeyError) as error:
        raise _malformed({"table": "human_decisions", "run_id": str(run_id)}, error) from error
    for event in history:
        if event.event_type != "runtime_node.human_wait_resolved":
            continue
        decision_id = event.state.get("decision_id")
        if isinstance(decision_id, str) and decision_id in decision_ids:
            continue
        action = event.state.get("decision_action", event.state.get("operator_decision"))
        if not isinstance(action, str):
            # Review findings use the same transition but grant no decision authority.
            continue
        decisions.append(
            {
                "decision_id": decision_id,
                "node_id": event.state.get("node_id"),
                "action": action,
                "reason": event.state.get("reason"),
                "evidence": event.evidence(),
            }
        )
        if isinstance(decision_id, str):
            decision_ids.add(decision_id)
    return {
        "recorded_decisions": decisions,
        "recorded_decision_count": len(decisions),
        "coverage": "ledger_recorded_decisions_only",
        "total_human_effort": _unknown("out-of-band work and decision effort are not measured"),
        "autonomous_delivery": _unknown("recorded lifecycle success does not establish autonomy"),
    }


def _measurements(attempt_events: list[_Event], implementation_ids: set[str]) -> dict[str, object]:
    latest: dict[str, _Event] = {}
    timing: dict[str, dict[str, object]] = {}
    costs: dict[str, dict[str, object]] = {}
    receipts: dict[str, dict[str, object]] = {}
    for event in attempt_events:
        latest[event.aggregate_id] = event
        state = event.state
        try:
            if "data" in state:
                attempt = node_attempt_from_dict(state)
                duration = attempt.duration_seconds
                cost = attempt.cost_amount
                for field, value in (("duration_seconds", duration), ("cost_amount", cost)):
                    if value is not None and (not math.isfinite(value) or value < 0):
                        raise ValueError(f"{field} must be finite and non-negative")
                if (
                    duration is not None
                    or attempt.started_at is not None
                    or attempt.completed_at is not None
                ):
                    started = None if attempt.started_at is None else attempt.started_at.isoformat()
                    completed = (
                        None if attempt.completed_at is None else attempt.completed_at.isoformat()
                    )
                    if started is not None:
                        _timestamp(started, event.evidence())
                    if completed is not None:
                        _timestamp(completed, event.evidence())
                    timing[event.aggregate_id] = {
                        "attempt_id": event.aggregate_id,
                        "duration_seconds": duration,
                        "started_at": started,
                        "completed_at": completed,
                        "evidence": event.evidence(),
                    }
                if cost is not None:
                    costs[event.aggregate_id] = {
                        "attempt_id": event.aggregate_id,
                        "cost_amount": cost,
                        "currency": None,
                        "evidence": event.evidence(),
                    }
            result = state.get("result")
            if result is not None and not isinstance(result, dict):
                raise ValueError("attempt result must be a mapping")
            if isinstance(result, dict) and "execution_receipt" in result:
                receipt = ExecutionReceipt.from_state(result["execution_receipt"])
                receipts[event.aggregate_id] = {
                    "attempt_id": event.aggregate_id,
                    **receipt.to_state(),
                    "evidence": event.evidence(),
                }
        except (InvalidInputError, ValueError, TypeError, KeyError, OverflowError) as error:
            raise _malformed(event.evidence(), error) from error
    count = len(latest.keys() | implementation_ids)

    def measurement(samples: dict[str, dict[str, object]], total_key: str) -> dict[str, object]:
        return {
            "state": "unknown"
            if not samples
            else "observed"
            if len(samples) == count
            else "partial",
            "samples": list(samples.values()),
            total_key: None,
            "coverage": {"attempts": count, "measured_attempts": len(samples)},
            "reason": (
                "recorded samples only; no complete run-total measurement or billing provenance"
            ),
        }

    return {
        "timing": measurement(timing, "total_duration_seconds"),
        "usage": {
            **_unknown("no structured token-usage producer in retained attempt records"),
            "coverage": {"attempts": count, "measured_attempts": 0},
        },
        "cost": measurement(costs, "total_cost_amount"),
        "effective_controls": {
            "state": (
                "unknown"
                if not receipts
                else "observed"
                if implementation_ids <= receipts.keys()
                else "partial"
            ),
            "coverage": {
                "implementation_attempts": len(implementation_ids),
                "receipts": len(receipts),
            },
            "receipts": list(receipts.values()),
            "missing_receipt_attempt_ids": sorted(implementation_ids - receipts.keys()),
            "reason": (
                "submitted controls are not effective observations; receipt UNKNOWNs are preserved"
            ),
        },
    }


def _recorded_repairs(
    request: Stage1RunRequest, events: list[_Event]
) -> dict[str, dict[str, object]]:
    """Identify repairs by the operation identity actually emitted by Stage 1."""
    repairs: dict[str, dict[str, object]] = {}
    first = _generation(request.implementation.attempt_id)
    if first is None:
        return repairs
    for event in events:
        state = event.state.get("data", event.state)
        if not isinstance(state, dict) or state.get("node_id") != "implement":
            continue
        attempt_id = state.get("attempt_id", state.get("id"))
        generation = _generation(attempt_id)
        if not isinstance(attempt_id, str) or generation is None or generation <= first:
            continue
        expected = OperationId.derive(
            run_id=request.run.id,
            node_id=NodeId("implement"),
            side_effect_kind="repair_implementation",
            target_scope=request.repository_id,
            ordinal=generation,
        )
        if state.get("operation_id") != str(expected):
            continue
        repair = repairs.setdefault(
            attempt_id,
            {
                "attempt_id": attempt_id,
                "generation": generation,
                "operation_id": str(expected),
                "evidence": [],
            },
        )
        evidence = repair["evidence"]
        assert isinstance(evidence, list)
        evidence.append(event.evidence())
    return repairs


def summarize_run(ledger: LedgerService, run_id: RunId) -> dict[str, object]:
    """Summarize one run using durable evidence only; never write or inspect the host."""
    source = {"aggregate_type": "run", "aggregate_id": str(run_id)}
    try:
        projection = ledger.read_projection(aggregate_type="run", aggregate_id=str(run_id))
        if projection is None:
            raise InvalidInputError(
                f"outcome summary run {run_id} was not found", details={"run_id": str(run_id)}
            )
        request = stage1_request_from_state(projection.state)
    except (ValueError, TypeError, KeyError) as error:
        raise _malformed(source, error) from error
    run_events = _events(ledger, "aggregate_type = 'run' AND aggregate_id = ?", (str(run_id),))
    if not run_events:
        raise _malformed(source, "run projection has no source events")
    for event in run_events:
        try:
            if stage1_request_from_state(event.state).digest != request.digest:
                raise ValueError("run event request differs from the durable projection")
        except (InvalidInputError, ValueError, TypeError, KeyError) as error:
            raise _malformed(event.evidence(), error) from error
    history = _events(
        ledger,
        "aggregate_type = 'runtime_node' AND substr(aggregate_id, 1, ?) = ?",
        (len(str(run_id)) + 1, f"{run_id}:"),
    )
    nodes: dict[str, _Event] = {}
    attempts: dict[str, dict[str, object]] = {}
    failures: list[dict[str, object]] = []
    holds: list[dict[str, object]] = []
    publications: list[dict[str, object]] = []
    implementation_ids: set[str] = set()
    for event in history:
        node_id = _text(event.state, "node_id", event.evidence())
        if event.state.get("run_id") != str(run_id) or event.aggregate_id != f"{run_id}:{node_id}":
            raise _malformed(event.evidence(), "runtime node identity differs from its run")
        status = _text(event.state, "status", event.evidence())
        attempt_id = _text(event.state, "attempt_id", event.evidence())
        nodes[node_id] = event
        attempt = attempts.setdefault(
            attempt_id,
            {
                "attempt_id": attempt_id,
                "node_id": node_id,
                "evidence": [],
            },
        )
        attempt["status"] = status
        evidence = attempt["evidence"]
        assert isinstance(evidence, list)
        evidence.append(event.evidence())
        if node_id == "implement":
            implementation_ids.add(attempt_id)
        detail = {
            "node_id": node_id,
            "attempt_id": attempt_id,
            "status": status,
            "reason": event.state.get("reason"),
            "repair": event.state.get("repair"),
            "evidence": event.evidence(),
        }
        if status in {"failed", "blocked", "cancelled"}:
            failures.append(detail)
        if status == "awaiting_human" or event.event_type == "runtime_node.evidence_held":
            holds.append(detail)
        if (
            node_id == "open_pr"
            and status == "passed"
            and event.event_type == "runtime_node.deterministic_completed"
            and "pull_request_number" in event.state
        ):
            publications.append(
                {
                    **detail,
                    **{
                        key: event.state.get(key)
                        for key in (
                            "pull_request_number",
                            "head_revision",
                            "base_revision",
                            "published_revision",
                        )
                    },
                }
            )
    known_ids = tuple(attempts)
    clauses = (
        "aggregate_type = 'node_attempt' AND (CASE WHEN json_valid(payload) THEN "
        "COALESCE(json_extract(payload, '$.run_id'), "
        "json_extract(payload, '$.data.run_id')) = ? END"
    )
    parameters: tuple[object, ...] = (str(run_id),)
    if known_ids:
        clauses += " OR aggregate_id IN (" + ",".join("?" for _ in known_ids) + ")"
        parameters += known_ids
    attempt_events = _events(ledger, clauses + ")", parameters)
    for event in attempt_events:
        raw = event.state.get("data", event.state)
        if not isinstance(raw, dict):
            raise _malformed(event.evidence(), "attempt data must be a mapping")
        node_id = _text(raw, "node_id", event.evidence())
        if raw.get("run_id") != str(run_id):
            raise _malformed(event.evidence(), "attempt run identity mismatch")
        if event.aggregate_id not in attempts:
            attempts[event.aggregate_id] = {
                "attempt_id": event.aggregate_id,
                "node_id": node_id,
                "status": raw.get("terminal_result", raw.get("state", "unknown")),
                "evidence": [],
            }
        if node_id == "implement":
            implementation_ids.add(event.aggregate_id)
        result = raw.get("result")
        if isinstance(result, dict):
            for field in ("head_revision", "execution_hold", "branch_verification_error"):
                if field in result:
                    attempts[event.aggregate_id][field] = result[field]
            if result.get("execution_hold") is not None:
                holds.append(
                    {
                        "node_id": node_id,
                        "attempt_id": event.aggregate_id,
                        "execution_hold": result["execution_hold"],
                        "evidence": event.evidence(),
                    }
                )
        evidence = attempts[event.aggregate_id]["evidence"]
        assert isinstance(evidence, list)
        evidence.append(event.evidence())
    measurements = _measurements(attempt_events, implementation_ids)
    # Existing projection queries remain useful as an explicitly current view. The history
    # above, rather than these replaceable projections, owns cumulative accounting.
    try:
        current_failures = list_failures(ledger, run_id=run_id, aggregate_type="runtime_node")
        current_interventions = list_interventions(
            ledger, run_id=run_id, aggregate_type="runtime_node"
        )
    except (InvalidInputError, ValueError, TypeError, KeyError) as error:
        raise _malformed(
            {"aggregate_type": "runtime_node", "run_id": str(run_id)}, error
        ) from error
    first_time = _timestamp(run_events[0].recorded_at, run_events[0].evidence())
    last_event = max(run_events + history + attempt_events, key=lambda event: event.commit_seq)
    last_time = _timestamp(last_event.recorded_at, last_event.evidence())
    measurements["event_history_span"] = {
        "state": "observed",
        "seconds": (last_time - first_time).total_seconds(),
        "evidence": [run_events[0].evidence(), last_event.evidence()],
        "meaning": "ledger recording span, not execution time or total human effort",
    }
    recorded_repairs = _recorded_repairs(request, history + attempt_events)
    return {
        "schema_version": 1,
        "run": {
            "run_id": str(run_id),
            "work_item_id": str(request.run.work_item_id),
            "repository_id": request.repository_id,
            "request_digest": str(request.digest),
            "workflow_definition_id": str(request.run.workflow_definition_id),
            "workflow_definition_digest": str(request.run.workflow_definition_digest),
            "work_item_snapshot_digest": str(request.run.work_item_snapshot_digest),
            "configuration_snapshot_digest": str(request.run.configuration_snapshot_digest),
            "capability_lock_digest": str(request.run.capability_lock_digest),
            "environment_manifest_digest": str(request.run.environment_manifest_digest),
            "policy_set_version": request.run.policy_set_version,
            "adapter_versions": dict(request.run.adapter_versions),
            "adapter_fingerprints": {
                name: str(digest) for name, digest in request.run.adapter_fingerprints.items()
            },
            "source_revision": request.work_snapshot.source_revision,
            "base_revision": request.run.base_revision,
            "base_branch": request.base_branch,
            "head_branch": request.head_branch,
            "evidence": [event.evidence() for event in run_events],
        },
        "accounting": {
            "task_count": 1,
            "native_completed_task_count": int(_native_complete(request, nodes)),
            "completion_basis": "stage1_merge_readiness",
            "attempt_count": len(attempts),
            "implementation_attempt_count": len(implementation_ids),
            "recorded_repair_count": len(recorded_repairs),
            "recorded_repairs": list(recorded_repairs.values()),
            "unclassified_implementation_attempt_ids": sorted(
                implementation_ids - recorded_repairs.keys() - {request.implementation.attempt_id}
            ),
            "repair_coverage": "persisted_stage1_repair_operation_identity_only",
            "repair_limit": request.repair_limit,
        },
        "attempts": list(attempts.values()),
        "lifecycle": {
            "status": projection.state.get("status"),
            "required_completion_nodes": sorted(_required_nodes(request)),
            "nodes": [
                {
                    "node_id": node_id,
                    "status": event.state.get("status"),
                    "attempt_id": event.state.get("attempt_id"),
                    "reason": event.state.get("reason"),
                    "repair": event.state.get("repair"),
                    "evidence": event.evidence(),
                }
                for node_id, event in sorted(nodes.items())
            ],
            "holds": holds,
            "failures": failures,
            "history": [
                {
                    "node_id": event.state.get("node_id"),
                    "status": event.state.get("status"),
                    "attempt_id": event.state.get("attempt_id"),
                    "event_type": event.event_type,
                    "evidence": event.evidence(),
                }
                for event in history
            ],
            "current_failure_node_ids": [record.node_id for record in current_failures],
            "current_intervention_node_ids": [record.node_id for record in current_interventions],
        },
        "native_publication": {
            "state": "published" if publications else "not_recorded",
            "publications": publications,
            "evidence": [publication["evidence"] for publication in publications],
            "meaning": (
                "native PR publication evidence only; not downstream merge or autonomous success"
            ),
        },
        "downstream": _downstream(ledger, run_id),
        "assistance": _assistance(ledger, run_id, history),
        "measurements": measurements,
    }
