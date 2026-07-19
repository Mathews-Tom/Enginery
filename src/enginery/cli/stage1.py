"""Durable Stage 1 lifecycle commands backed by the coordinator runtime."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import RunId
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE, CoordinatorRuntime
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1RunService, stage1_request_from_state

_HEARTBEAT_WINDOW = timedelta(seconds=60)


def run_stage1(args: argparse.Namespace) -> int:
    """Run one Stage 1 lifecycle operation and emit a machine-readable result."""
    command = args.stage1_command
    if command is None:
        raise InvalidInputError("stage1 requires a subcommand")
    if command == "start":
        _start(args)
    elif command == "watch":
        _watch(args)
    elif command in {"approve", "reject"}:
        _resolve_human_wait(args, approved=command == "approve")
    elif command == "cancel":
        _cancel(args)
    elif command == "resume":
        _resume(args)
    elif command == "evidence":
        _evidence(args)
    else:  # pragma: no cover - argparse restricts command values
        raise AssertionError(f"unhandled Stage 1 command: {command}")
    return 0


def _start(args: argparse.Namespace) -> None:
    raw_request = _read_json(args.request)
    request = stage1_request_from_state(raw_request)
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        run = Stage1RunService(runtime=runtime, ledger=ledger).start(
            request,
            now=_now(),
            heartbeat_window=_HEARTBEAT_WINDOW,
        )
        _print({"run_id": str(request.run.id), "status": run.status.value})
    finally:
        ledger.close()


def _watch(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        service = Stage1RunService(runtime=runtime, ledger=ledger)
        previous = service.next_action(RunId(args.run_id))
        if args.advance:
            service = _advancing_service(args, runtime=runtime, ledger=ledger)
            progression = service.advance(
                RunId(args.run_id),
                now=_now(),
                heartbeat_window=_HEARTBEAT_WINDOW,
                lease_window=timedelta(seconds=30),
                limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            )
        else:
            progression = previous
        run = progression.run
        _print(
            {
                "run_id": str(run.request.run.id),
                "status": run.status.value,
                "aggregate_version": run.aggregate_version,
                "action_taken": previous.action.value if args.advance else None,
                "next_action": progression.action.value,
                "nodes": _nodes(ledger, run_id=str(run.request.run.id)),
            }
        )
    finally:
        ledger.close()


def _advancing_service(
    args: argparse.Namespace, *, runtime: CoordinatorRuntime, ledger: LedgerService
) -> Stage1RunService:
    github_repository = _required_option(args.github_repository, "--github-repository")
    github_credential_reference = _required_option(
        args.github_credential_reference, "--github-credential-reference"
    )
    omp_credential_reference = _required_option(
        args.omp_credential_reference, "--omp-credential-reference"
    )
    if args.artifact_root is None:
        raise InvalidInputError("stage1 watch --advance requires --artifact-root")
    github = GitHubAdapterConfig(
        repository=github_repository,
        credential_reference=github_credential_reference,
        executable=args.github_executable,
    )
    harness = OmpHarness(
        OmpAdapterConfig(
            credential_reference=omp_credential_reference,
            executable=args.omp_executable,
        ),
        ArtifactStore(args.artifact_root),
    )
    return Stage1RunService(
        runtime=runtime,
        ledger=ledger,
        work_ledger=GitHubWorkLedger(github),
        omp_harness=harness,
    )


def _required_option(value: object, option: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"stage1 watch --advance requires {option}")
    return value


def _resolve_human_wait(args: argparse.Namespace, *, approved: bool) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        epoch = runtime.claim_epoch(now=_now(), heartbeat_window=_HEARTBEAT_WINDOW)
        runtime.resolve_human_wait(
            run_id=args.run_id,
            node_id=args.node_id,
            epoch=epoch.epoch,
            now=_now(),
            outcome="passed" if approved else "failed",
            extra={
                "operator_decision": "approved" if approved else "rejected",
                "reason": args.reason,
            },
        )
        _print(
            {
                "run_id": args.run_id,
                "node_id": args.node_id,
                "status": "passed" if approved else "failed",
            }
        )
    finally:
        ledger.close()


def _cancel(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        epoch = runtime.claim_epoch(now=_now(), heartbeat_window=_HEARTBEAT_WINDOW)
        runtime.cancel_node(run_id=args.run_id, node_id=args.node_id, epoch=epoch.epoch, now=_now())
        _print({"run_id": args.run_id, "node_id": args.node_id, "status": "cancelled"})
    finally:
        ledger.close()


def _resume(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        epoch = runtime.claim_epoch(now=_now(), heartbeat_window=_HEARTBEAT_WINDOW)
        request = runtime.read_node_request(run_id=args.run_id, node_id=args.node_id)
        runtime.resume_human_wait(
            request=replace(
                request,
                attempt_id=args.attempt_id,
                operation_id=args.operation_id,
                expected_attempt_version=0,
            ),
            epoch=epoch.epoch,
            now=_now(),
        )
        _print({"run_id": args.run_id, "node_id": args.node_id, "status": "queued"})
    finally:
        ledger.close()


def _evidence(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        run = Stage1RunService(
            runtime=CoordinatorRuntime(ledger, owner=args.owner), ledger=ledger
        ).read(RunId(args.run_id))
        _print(
            {
                "run_id": str(run.request.run.id),
                "run_status": run.status.value,
                "request_digest": str(run.request.digest),
                "source_revision": run.request.work_snapshot.source_revision,
                "base_revision": run.request.run.base_revision,
                "nodes": _nodes(ledger, run_id=str(run.request.run.id)),
            }
        )
    finally:
        ledger.close()


def _nodes(ledger: LedgerService, *, run_id: str) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    prefix = f"{run_id}:"
    for projection in ledger.list_projections(aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE):
        if not projection.aggregate_id.startswith(prefix):
            continue
        state = projection.state
        nodes.append(
            {
                "node_id": projection.aggregate_id.removeprefix(prefix),
                "attempt_id": state.get("attempt_id"),
                "operation_id": state.get("operation_id"),
                "status": state.get("status"),
                "artifacts": state.get("artifact_references", []),
            }
        )
    return sorted(nodes, key=lambda node: str(node["node_id"]))


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
