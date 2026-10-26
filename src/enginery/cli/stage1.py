"""Durable engine lifecycle commands backed by the coordinator runtime."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from enginery.cli.factory_decisions import decide_in_ledger
from enginery.cli.stage1_composition import (
    HEARTBEAT_WINDOW,
    advancing_service,
    lease_window,
    nodes,
    read_only_service,
    runtime_for_run,
    start_bound_run,
    watch_document,
)
from enginery.cli.stage1_execution import execution_report
from enginery.cli.stage1_request import run_build_request
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import RunId
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.review import ReviewFinding, ReviewReport
from enginery.workflows.run_lifecycle import Stage1RunService, stage1_request_from_state


def run_stage1(args: argparse.Namespace) -> int:
    """Run one engine lifecycle operation and emit a machine-readable result."""
    command = args.stage1_command
    if command == "start":
        _start(args)
    elif command == "next":
        _watch(args)
    elif command == "review":
        _review(args)
    elif command in {"approve", "reject"}:
        _resolve_human_wait(args, approved=command == "approve")
    elif command == "cancel":
        _cancel(args)
    elif command == "evidence":
        _evidence(args)
    elif command == "build-request":
        return run_build_request(args)
    else:  # pragma: no cover - argparse restricts command values
        raise AssertionError(f"unhandled Stage 1 command: {command}")
    return 0


def _start(args: argparse.Namespace) -> None:
    raw_request = _read_json(args.request)
    request = stage1_request_from_state(raw_request)
    ledger = LedgerService.open(args.database)
    try:
        run = start_bound_run(ledger, owner=args.owner, request=request, raw_request=raw_request)
        _print({"run_id": str(run.request.run.id), "status": run.status.value})
    finally:
        ledger.close()


def _watch(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = runtime_for_run(ledger, owner=args.owner, run_id=args.run_id)
        request = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(args.run_id)).request
        service = (
            advancing_service(runtime=runtime, ledger=ledger, request=request)
            if args.advance
            else read_only_service(runtime=runtime, ledger=ledger, request=request)
        )
        previous = service.next_action(RunId(args.run_id))
        if args.advance:
            progression = service.advance(
                RunId(args.run_id),
                now=_now(),
                heartbeat_window=HEARTBEAT_WINDOW,
                lease_window=lease_window(previous.run.request),
                limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            )
        else:
            progression = previous
        _print(
            watch_document(
                ledger,
                runtime,
                progression,
                action_taken=previous.action.value if args.advance else None,
            )
        )
    finally:
        ledger.close()


def _resolve_human_wait(args: argparse.Namespace, *, approved: bool) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = runtime_for_run(ledger, owner=args.owner, run_id=args.run_id)
        request = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(args.run_id)).request
        action = (
            "pull_request.open"
            if approved and args.node_id == "open_pr"
            else f"human_wait.{args.node_id}.{'approve' if approved else 'reject'}"
        )
        result = decide_in_ledger(
            ledger,
            request,
            action=action,
            subject_digest=args.subject_digest,
            challenge=args.challenge,
            principal_id=args.principal_id,
            reason=args.reason,
            owner=args.owner,
        )
        _print({**result, "status": "passed" if approved else "failed"})
    finally:
        ledger.close()


def _review(args: argparse.Namespace) -> None:
    report = _review_report_from_state(_read_json(args.report))
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        service = read_only_service(
            runtime=runtime,
            ledger=ledger,
            request=Stage1RunService(runtime=runtime, ledger=ledger)
            .read(RunId(args.run_id))
            .request,
        )
        run = service.read(RunId(args.run_id))
        result = service.review_implementation(
            run.request,
            report,
            repair_attempt=args.repair_attempt,
            now=_now(),
            heartbeat_window=HEARTBEAT_WINDOW,
        )
        _print(
            {
                "run_id": str(run.request.run.id),
                "review_outcome": result.outcome.value,
                "findings_recorded": True,
                "authority_granted": False,
            }
        )
    finally:
        ledger.close()


def _review_report_from_state(state: object) -> ReviewReport:
    if not isinstance(state, dict):
        raise InvalidInputError("Stage 1 review report must be a mapping")
    producer = _required_string(state, "producer")
    reviewer = _required_string(state, "reviewer")
    findings_value = state.get("findings")
    if not isinstance(findings_value, list):
        raise InvalidInputError("Stage 1 review findings must be a list")
    findings = tuple(_review_finding_from_state(value) for value in findings_value)
    return ReviewReport(producer=producer, reviewer=reviewer, findings=findings)


def _review_finding_from_state(value: object) -> ReviewFinding:
    if not isinstance(value, dict):
        raise InvalidInputError("Stage 1 review finding must be a mapping")
    actionable = value.get("actionable")
    blocking = value.get("blocking")
    if not isinstance(actionable, bool) or not isinstance(blocking, bool):
        raise InvalidInputError("Stage 1 review finding flags must be booleans")
    return ReviewFinding(
        finding_id=_required_string(value, "finding_id"),
        actionable=actionable,
        blocking=blocking,
    )


def _required_string(state: dict[str, object], field_name: str) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"Stage 1 review {field_name} must be non-blank")
    return value


def _cancel(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = runtime_for_run(ledger, owner=args.owner, run_id=args.run_id)
        epoch = runtime.claim_epoch(now=_now(), heartbeat_window=HEARTBEAT_WINDOW)
        runtime.cancel_node(run_id=args.run_id, node_id=args.node_id, epoch=epoch.epoch, now=_now())
        _print({"run_id": args.run_id, "node_id": args.node_id, "status": "cancelled"})
    finally:
        ledger.close()


def _evidence(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(args.run_id))
        progression = read_only_service(
            runtime=runtime, ledger=ledger, request=run.request
        ).next_action(RunId(args.run_id))
        _print(
            {
                "run_id": str(run.request.run.id),
                "run_status": run.status.value,
                "request_digest": str(run.request.digest),
                "source_revision": run.request.work_snapshot.source_revision,
                "base_revision": run.request.run.base_revision,
                "nodes": nodes(ledger, runtime, run_id=str(run.request.run.id)),
                "hold": None if progression.hold is None else progression.hold.to_state(),
                "execution": execution_report(
                    ledger,
                    run,
                    hold=progression.hold,
                    observed_at_resume=progression.observed_host_version,
                ),
            }
        )
    finally:
        ledger.close()


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise InvalidInputError(
            "unable to read Stage 1 request", details={"path": str(path)}
        ) from error
    except UnicodeDecodeError as error:
        raise InvalidInputError(
            "Stage 1 request must be UTF-8", details={"path": str(path)}
        ) from error
    except json.JSONDecodeError as error:
        raise InvalidInputError(
            "Stage 1 request must be JSON", details={"path": str(path)}
        ) from error
    if not isinstance(raw, dict):
        raise InvalidInputError("Stage 1 request must be a JSON object")
    return raw


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _now() -> datetime:
    return datetime.now(tz=UTC)


__all__ = ["run_stage1"]
