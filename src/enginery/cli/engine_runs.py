"""Durable engine lifecycle commands backed by the coordinator runtime."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from enginery.cli._exit_codes import exit_code_for
from enginery.cli.execution_diagnostics import execution_report
from enginery.cli.factory_decisions import decide_in_ledger
from enginery.cli.request_compiler import run_build_request
from enginery.cli.run_composition import (
    HEARTBEAT_WINDOW,
    advancing_service,
    lease_window,
    nodes,
    read_only_service,
    runtime_for_run,
    start_bound_run,
    watch_document,
)
from enginery.cli.setup_files import refuse_links
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    FailureClass,
    InvalidInputError,
    MissingPrerequisiteError,
    ValidationFailureError,
)
from enginery.domain.ids import RunId
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE, CoordinatorRuntime
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import ArtifactDigestMismatchError, ArtifactMissingError
from enginery.ledger.service import LedgerService
from enginery.workflows.review import ReviewFinding, ReviewReport
from enginery.workflows.run_intent import stage1_request_from_state
from enginery.workflows.run_lifecycle import Stage1RunService


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
        return _evidence(args)
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


def _evidence(args: argparse.Namespace) -> int:
    verify = bool(getattr(args, "verify", False))
    ledger = _open_read_only(args.database) if verify else LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(args.run_id))
        progression = read_only_service(
            runtime=runtime, ledger=ledger, request=run.request
        ).next_action(RunId(args.run_id))
        payload: dict[str, object] = {
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
        intact = True
        if verify:
            verification = _verify_artifacts(
                ledger,
                run_id=str(run.request.run.id),
                artifact_root=run.request.execution_configuration.artifact_root,
            )
            payload["verification"] = verification
            summary = verification["summary"]
            assert isinstance(summary, dict)
            intact = summary["missing"] == 0 and summary["tampered"] == 0
        _print(payload)
        return 0 if intact else exit_code_for(FailureClass.VALIDATION_FAILURE)
    finally:
        ledger.close()


def _open_read_only(database: Path) -> LedgerService:
    """Open an existing ledger without any ability to write or create it."""
    path = database.expanduser().absolute()
    refuse_links(path.parent, Path(path.name), what="Stage 1 ledger")
    if not path.is_file():
        raise MissingPrerequisiteError(
            "engine evidence --verify requires an existing engine ledger",
            details={"path": str(path)},
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=1")
        # Reading a WAL ledger needs its shared-memory file; SQLite creates it beside the
        # ledger, so an unwritable directory fails on the first read, not on connect.
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.DatabaseError as error:
        if connection is not None:
            connection.close()
        remedy = (
            "; a WAL ledger can only be read where SQLite may create its shared-memory file, "
            "so make the ledger's directory writable"
            if error.sqlite_errorname == "SQLITE_READONLY_DIRECTORY"
            else ""
        )
        raise MissingPrerequisiteError(
            f"engine evidence --verify cannot read the ledger read-only at {path}: {error}"
            + remedy,
            details={"path": str(path), "sqlite_error": str(error)},
        ) from error
    connection.row_factory = sqlite3.Row
    return LedgerService(connection)


_NODE_DIGEST_KEYS = ("validation_artifact_digest", "evidence_artifact_digest")


def _recorded_artifacts(ledger: LedgerService, run_id: str) -> list[tuple[str, object]]:
    """Every ``(artifact id, recorded digest)`` the run's ledger records reference."""
    recorded: list[tuple[str, object]] = [
        (str(row["artifact_id"]), row["digest"])
        for row in ledger.connection.execute(
            "SELECT artifact_id, digest FROM artifacts WHERE run_id = ? ORDER BY artifact_id",
            (run_id,),
        )
    ]
    prefix = f"{run_id}:"
    for projection in ledger.list_projections(aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE):
        if not projection.aggregate_id.startswith(prefix):
            continue
        node_id = projection.aggregate_id.removeprefix(prefix)
        state = projection.state
        for key in _NODE_DIGEST_KEYS:
            if state.get(key) is not None:
                recorded.append((f"node:{node_id}:{key}", state[key]))
        references = state.get("artifact_references", [])
        if not isinstance(references, list):
            raise ValidationFailureError(
                "recorded artifact references are malformed",
                details={"run_id": run_id, "node_id": node_id},
            )
        recorded.extend(
            (f"node:{node_id}:artifact_references:{index}", reference)
            for index, reference in enumerate(references)
        )
    return recorded


def _verify_artifacts(
    ledger: LedgerService, *, run_id: str, artifact_root: Path
) -> dict[str, object]:
    """Re-hash the stored bytes of every artifact the run references; never writes."""
    if not artifact_root.is_dir():
        raise MissingPrerequisiteError(
            "the run's artifact store root does not exist",
            details={"artifact_root": str(artifact_root)},
        )
    store = ArtifactStore(artifact_root, create_directories=False)
    results: list[dict[str, str]] = []
    summary = {"intact": 0, "missing": 0, "tampered": 0}
    for artifact_id, recorded in _recorded_artifacts(ledger, run_id):
        text = recorded if isinstance(recorded, str) else ""
        algorithm, _, hex_value = text.partition(":")
        try:
            digest = Digest(algorithm=algorithm, hex_value=hex_value)
        except InvalidInputError as error:
            raise ValidationFailureError(
                "run references a malformed artifact digest",
                details={"artifact_id": artifact_id, "digest": repr(recorded)},
            ) from error
        try:
            store.read_bytes(digest)
            status = "intact"
        except ArtifactMissingError:
            status = "missing"
        except ArtifactDigestMismatchError:
            status = "tampered"
        summary[status] += 1
        results.append({"artifact_id": artifact_id, "digest": str(digest), "status": status})
    return {"artifact_root": str(artifact_root), "artifacts": results, "summary": summary}


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
