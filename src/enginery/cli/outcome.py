"""``enginery outcome``: read-only inspection of raw outcome observations.

Registration and capture happen through the coordinator-owned Stage 1
progression service and, for the escaped-defect and reopened-issue kinds
with no adapter read signal, an explicit human capture call -- neither is
a CLI-driven mutation. This module's job is the same safe operator
inspection role ``enginery stage2 status`` already established: list what
is pending, captured, or expired, and report the versioned completeness
derivation over it.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ObservationId, RunId
from enginery.domain.observation import ObservationRequest, ObservationState
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.evaluation.queries import list_failures, list_interventions
from enginery.ledger.service import LedgerService


def run_outcome(args: argparse.Namespace) -> int:
    """Run one ``outcome`` command and emit a machine-readable result."""
    command = args.outcome_command
    if command is None:
        raise InvalidInputError("outcome requires a subcommand")
    if command == "list":
        _list(args)
    elif command == "show":
        _show(args)
    elif command == "completeness":
        _report_completeness(args)
    elif command == "interventions":
        _interventions(args)
    elif command == "failures":
        _failures(args)
    else:  # pragma: no cover - argparse restricts command values
        raise AssertionError(f"unhandled outcome command: {command}")
    return 0


def _list(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        service = OutcomeCaptureService(ledger=ledger)
        state = ObservationState(args.state) if args.state is not None else None
        observations = service.list_observations(state=state)
        _print(
            {"observations": [_observation_payload(observation) for observation in observations]}
        )
    finally:
        ledger.close()


def _show(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        service = OutcomeCaptureService(ledger=ledger)
        observation = service.read_observation(ObservationId(args.observation_id))
        if observation is None:
            _print({"observation_id": args.observation_id, "found": False})
            return
        payload = _observation_payload(observation)
        payload["found"] = True
        if observation.outcome_id is not None:
            outcome = service.read_outcome(observation.outcome_id)
            if outcome is not None:
                payload["outcome"] = {
                    "id": str(outcome.id),
                    "kind": outcome.kind.value,
                    "observed_at": outcome.observed_at.isoformat(),
                    "detail": dict(outcome.detail),
                }
        _print(payload)
    finally:
        ledger.close()


def _report_completeness(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        service = OutcomeCaptureService(ledger=ledger)
        report = service.completeness(reference_time=datetime.now(tz=UTC))
        _print(
            {
                "derivation_version": report.derivation_version,
                "captured": report.captured,
                "indeterminate": report.indeterminate,
                "pending": report.pending,
                "completeness": report.completeness,
            }
        )
    finally:
        ledger.close()


def _interventions(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        interventions = list_interventions(
            ledger, run_id=RunId(args.run_id), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
        )
        _print(
            {
                "interventions": [
                    {
                        "run_id": intervention.run_id,
                        "node_id": intervention.node_id,
                        "decision": intervention.decision,
                        "reason": intervention.reason,
                        "status": intervention.status,
                    }
                    for intervention in interventions
                ]
            }
        )
    finally:
        ledger.close()


def _failures(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        failures = list_failures(
            ledger, run_id=RunId(args.run_id), aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE
        )
        _print(
            {
                "failures": [
                    {
                        "run_id": failure.run_id,
                        "node_id": failure.node_id,
                        "detail": dict(failure.detail),
                    }
                    for failure in failures
                ]
            }
        )
    finally:
        ledger.close()


def _observation_payload(observation: ObservationRequest) -> dict[str, object]:
    return {
        "id": str(observation.id),
        "work_item_id": str(observation.work_item_id),
        "run_id": str(observation.run_id),
        "kind": observation.kind.value,
        "state": observation.state.value,
        "opened_at": observation.opened_at.isoformat(),
        "due_at": observation.due_at.isoformat(),
        "resolved_at": observation.resolved_at.isoformat()
        if observation.resolved_at is not None
        else None,
        "outcome_id": str(observation.outcome_id) if observation.outcome_id is not None else None,
        "detail": dict(observation.detail),
    }


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["run_outcome"]
