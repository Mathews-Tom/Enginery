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

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.domain.errors import FailureClass, InvalidInputError
from enginery.domain.g4_authority_evidence import (
    G4AuthorityEvidence,
    g4_authority_evidence_from_state,
)
from enginery.domain.g4_deficiency import g4_deficiency_finding_from_state
from enginery.engine.runtime import RUN_AGGREGATE_TYPE, RUNTIME_NODE_AGGREGATE_TYPE
from enginery.evaluation.gate import G4Inputs, GateReport, evaluate_g4
from enginery.evaluation.gate_floor import load_gate_floor_config
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.evaluation.queries import list_all_interventions
from enginery.ledger.g4_authority_evidence import (
    G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE,
    record_g4_authority_evidence,
)
from enginery.ledger.g4_deficiency import G4_DEFICIENCY_AGGREGATE_TYPE
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
    if command == "record-g4-deficiency-evidence":
        return _record_g4_deficiency_evidence(args)
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


def _record_g4_deficiency_evidence(args: argparse.Namespace) -> int:
    floor = load_gate_floor_config(args.floor_config)
    principal_github_logins = dict(floor.github_login_by_principal_id)
    ledger = LedgerService.open(args.database)
    try:
        projection = ledger.read_projection(
            aggregate_type=G4_DEFICIENCY_AGGREGATE_TYPE, aggregate_id=args.finding_id
        )
        if projection is None:
            raise InvalidInputError(
                "G4 deficiency finding does not exist",
                details={"finding_id": args.finding_id},
            )
        finding = g4_deficiency_finding_from_state(projection.state)
        adapter = GitHubWorkLedger(
            GitHubAdapterConfig(
                repository=args.github_repository,
                credential_reference=args.github_credential_reference,
                executable=args.github_executable,
            )
        )
        evidence = adapter.verify_g4_evidence(
            finding=finding,
            principal_github_logins=principal_github_logins,
            verified_at=datetime.now(tz=UTC),
        )
        record_g4_authority_evidence(ledger, evidence=evidence, correlation_id=args.correlation_id)
    finally:
        ledger.close()
    _print_authority_evidence(evidence, as_json=args.json)
    return SUCCESS


def _g4_inputs(ledger: LedgerService) -> G4Inputs:
    requests: tuple[Stage1RunRequest, ...] = tuple(
        stage1_request_from_state(record.state)
        for record in ledger.list_projections(aggregate_type=RUN_AGGREGATE_TYPE)
    )
    completed = tuple(
        request for request in requests if _verify_passed(ledger, run_id=str(request.run.id))
    )
    classified_completed = tuple(
        request
        for request in completed
        if request.work_snapshot.classification_provenance is not None
    )
    interventions = list_all_interventions(ledger, aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE)
    completeness = OutcomeCaptureService(ledger=ledger).completeness(
        reference_time=datetime.now(tz=UTC)
    )
    return G4Inputs(
        completed_run_count=len(classified_completed),
        completed_workflow_type_count=len(
            {request.work_snapshot.work_item.work_kind for request in classified_completed}
        ),
        completed_risk_class_count=len(
            {request.work_snapshot.work_item.risk_class for request in classified_completed}
        ),
        intervention_with_reason_count=sum(
            1 for intervention in interventions if intervention.reason
        ),
        completeness=completeness,
        repository_count=len({request.run.repository for request in classified_completed}),
        eligible_classified_completed_run_ids=tuple(
            str(request.run.id) for request in classified_completed
        ),
        deficiency_findings=tuple(
            g4_deficiency_finding_from_state(record.state)
            for record in ledger.list_projections(aggregate_type=G4_DEFICIENCY_AGGREGATE_TYPE)
        ),
        authority_evidence=tuple(
            g4_authority_evidence_from_state(record.state)
            for record in ledger.list_projections(
                aggregate_type=G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE
            )
        ),
    )


def _verify_passed(ledger: LedgerService, *, run_id: str) -> bool:
    projection = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:{_VERIFY_NODE_ID}"
    )
    if projection is None:
        return False
    return projection.state.get("status") == _VERIFY_PASSED_STATUS


def _print_authority_evidence(evidence: G4AuthorityEvidence, *, as_json: bool) -> None:
    serialized = evidence.to_state()
    if as_json:
        print(json.dumps(serialized, indent=2, sort_keys=True))
        return
    print(
        "recorded G4 authority evidence for "
        f"{serialized['finding_id']} from pull request #{serialized['pull_request_number']}"
    )


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
