"""``enginery gate status``: deterministic readiness reporting against a
registered decision gate.

Read-only over durable ledger state, M14a's existing outcome-capture
completeness projection, and a human-maintained floor/roster
configuration file. This command performs no side effect and cannot
itself satisfy any gate condition -- see :mod:`enginery.evaluation.gate`
for the fail-closed evaluation this command reports.

Gate G4's "completed run" signal is the same durable evidence
``verify_merge_ready`` already gates PR publication on -- the
``"{run_id}:verify"`` runtime node reaching a ``passed`` status -- not a
``Run.state`` mutation, since nothing in this codebase ever persists a
``Run`` transitioning to ``succeeded``. Every registered Stage 1 run's
bound ``WorkItem`` and repository are read from the same durable
``"run"`` aggregate payload Stage 1 already writes once at registration.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.domain.errors import FailureClass, InvalidInputError
from enginery.engine.runtime import RUN_AGGREGATE_TYPE, RUNTIME_NODE_AGGREGATE_TYPE
from enginery.evaluation.gate import G4Inputs, GateReport, evaluate_g4
from enginery.evaluation.gate_floor import load_gate_floor_config
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.evaluation.queries import list_all_interventions
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1RunRequest, stage1_request_from_state

_SUPPORTED_GATES = frozenset({"G4"})
_VERIFY_NODE_ID = "verify"
_VERIFY_PASSED_STATUS = "passed"


def run_gate(args: argparse.Namespace) -> int:
    """Run one ``gate`` command and emit a machine-readable result."""
    command = args.gate_command
    if command is None:
        raise InvalidInputError("gate requires a subcommand")
    if command == "status":
        return _status(args)
    raise AssertionError(f"unhandled gate command: {command}")  # pragma: no cover


def _status(args: argparse.Namespace) -> int:
    if args.gate not in _SUPPORTED_GATES:
        raise InvalidInputError(f"unsupported gate {args.gate!r}", details={"gate": args.gate})
    floor = load_gate_floor_config(args.floor_config)
    ledger = LedgerService.open(args.database)
    try:
        report = evaluate_g4(floor=floor, inputs=_g4_inputs(ledger))
    finally:
        ledger.close()
    _print(report, as_json=args.json)
    return SUCCESS if report.passed else exit_code_for(FailureClass.MISSING_PREREQUISITE)


def _g4_inputs(ledger: LedgerService) -> G4Inputs:
    requests: tuple[Stage1RunRequest, ...] = tuple(
        stage1_request_from_state(record.state)
        for record in ledger.list_projections(aggregate_type=RUN_AGGREGATE_TYPE)
    )
    completed = tuple(
        request for request in requests if _verify_passed(ledger, run_id=str(request.run.id))
    )
    interventions = list_all_interventions(ledger, aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE)
    completeness = OutcomeCaptureService(ledger=ledger).completeness(
        reference_time=datetime.now(tz=UTC)
    )
    return G4Inputs(
        completed_run_count=len(completed),
        completed_workflow_type_count=len(
            {request.work_snapshot.work_item.work_kind for request in completed}
        ),
        completed_risk_class_count=len(
            {request.work_snapshot.work_item.risk_class for request in completed}
        ),
        intervention_with_reason_count=sum(
            1 for intervention in interventions if intervention.reason
        ),
        completeness=completeness,
        repository_count=len({request.run.repository for request in requests}),
    )


def _verify_passed(ledger: LedgerService, *, run_id: str) -> bool:
    projection = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:{_VERIFY_NODE_ID}"
    )
    if projection is None:
        return False
    return projection.state.get("status") == _VERIFY_PASSED_STATUS


def _print(report: GateReport, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "gate": report.gate,
            "overall": "pass" if report.passed else "fail",
            "conditions": [
                {
                    "id": condition.id,
                    "status": condition.status.value,
                    "detail": condition.detail,
                    "metrics": dict(condition.metrics),
                }
                for condition in report.conditions
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"gate {report.gate}: {'pass' if report.passed else 'fail'}")
    for condition in report.conditions:
        print(f"[{condition.status.value}] {condition.id}: {condition.detail}")


__all__ = ["run_gate"]
