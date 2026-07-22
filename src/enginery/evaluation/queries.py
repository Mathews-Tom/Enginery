"""Read-only intervention and failure queries over durable run history.

Human interventions (approve/reject decisions on a waiting node) and node
failures are already recorded durably today through the coordinator
runtime's own ``runtime_node`` projections -- ``resolve_human_wait``
persists the operator's decision and rationale as
``runtime_node.human_wait_resolved``, and any node's terminal ``failed``
status is a normal projection field. This module adds no new recording
pipeline; it only reads that existing history back out in a queryable
shape, matching the evaluation-signal role design.md's domain model
assigns to interventions.

``aggregate_type`` is a caller-supplied parameter rather than an import of
``enginery.engine.runtime.RUNTIME_NODE_AGGREGATE_TYPE``: the evaluation
layer may depend on ``ledger``, but not on ``engine`` (see
``scripts/check_import_boundaries.py``), so the engine-owned constant is
passed in by callers -- such as the CLI -- that already sit above both
layers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from enginery.domain.ids import RunId
from enginery.ledger.projections import ProjectionRecord
from enginery.ledger.service import LedgerService

_EXCLUDED_DETAIL_KEYS = frozenset({"run_id", "node_id", "status"})


@dataclass(frozen=True, slots=True)
class InterventionRecord:
    """One durable human decision on a waiting run node."""

    run_id: str
    node_id: str
    decision: str
    reason: str | None
    status: str


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """One durable failed run node."""

    run_id: str
    node_id: str
    detail: Mapping[str, object]


def list_interventions(
    ledger: LedgerService, *, run_id: RunId, aggregate_type: str
) -> tuple[InterventionRecord, ...]:
    """Every recorded human decision for ``run_id``, oldest evidence first
    by node projection order. A node with no ``operator_decision`` field
    (every non-human-wait node) is not an intervention and is excluded."""
    interventions: list[InterventionRecord] = []
    for record in _nodes_for_run(ledger, run_id, aggregate_type=aggregate_type):
        decision = record.state.get("operator_decision")
        if not isinstance(decision, str):
            continue
        reason = record.state.get("reason")
        interventions.append(
            InterventionRecord(
                run_id=str(run_id),
                node_id=_node_id(record.aggregate_id),
                decision=decision,
                reason=reason if isinstance(reason, str) else None,
                status=str(record.state.get("status", "")),
            )
        )
    return tuple(interventions)


def list_all_interventions(
    ledger: LedgerService, *, aggregate_type: str
) -> tuple[InterventionRecord, ...]:
    """Every recorded human decision across every run, ledger-wide.

    Same recording contract as :func:`list_interventions` -- a node with
    no ``operator_decision`` field is not an intervention and is
    excluded -- but scoped to the whole ledger rather than one
    ``run_id``, for callers (such as a gate-readiness report) that need
    an aggregate count across every run rather than one run's history.
    """
    interventions: list[InterventionRecord] = []
    for record in ledger.list_projections(aggregate_type=aggregate_type):
        decision = record.state.get("operator_decision")
        if not isinstance(decision, str):
            continue
        reason = record.state.get("reason")
        interventions.append(
            InterventionRecord(
                run_id=_run_id(record.aggregate_id),
                node_id=_node_id(record.aggregate_id),
                decision=decision,
                reason=reason if isinstance(reason, str) else None,
                status=str(record.state.get("status", "")),
            )
        )
    return tuple(interventions)


def list_failures(
    ledger: LedgerService, *, run_id: RunId, aggregate_type: str
) -> tuple[FailureRecord, ...]:
    """Every node that reached a durable ``failed`` status for ``run_id``."""
    failures: list[FailureRecord] = []
    for record in _nodes_for_run(ledger, run_id, aggregate_type=aggregate_type):
        if record.state.get("status") != "failed":
            continue
        detail = {
            key: value for key, value in record.state.items() if key not in _EXCLUDED_DETAIL_KEYS
        }
        failures.append(
            FailureRecord(run_id=str(run_id), node_id=_node_id(record.aggregate_id), detail=detail)
        )
    return tuple(failures)


def _nodes_for_run(
    ledger: LedgerService, run_id: RunId, *, aggregate_type: str
) -> tuple[ProjectionRecord, ...]:
    prefix = f"{run_id}:"
    records = ledger.list_projections(aggregate_type=aggregate_type)
    return tuple(record for record in records if record.aggregate_id.startswith(prefix))


def _node_id(aggregate_id: str) -> str:
    return aggregate_id.split(":", 1)[1] if ":" in aggregate_id else aggregate_id


def _run_id(aggregate_id: str) -> str:
    return aggregate_id.split(":", 1)[0] if ":" in aggregate_id else aggregate_id


__all__ = [
    "FailureRecord",
    "InterventionRecord",
    "list_all_interventions",
    "list_failures",
    "list_interventions",
]
