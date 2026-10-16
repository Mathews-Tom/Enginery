"""Manual factory commands composed over the authoritative Stage 1 lifecycle."""

from __future__ import annotations

import argparse
import math
import shlex
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from enginery.application.effective_config import EffectiveConfig
from enginery.application.factory_intake import IntakeEntry, RepositoryIntake, derive_run_id
from enginery.cli.config_locations import private_state_directory, project_identity
from enginery.cli.factory_collect import (
    database_path,
    github_ledger,
    index_path,
    intake_lock,
    intake_prerequisites,
    paused_hold,
    read_document,
    read_index,
    read_ledger,
    repository_name,
    require_state_path,
    write_document,
)
from enginery.cli.factory_setup import LOCKFILE_RELATIVE, load_project_configuration
from enginery.cli.stage1_composition import (
    HEARTBEAT_WINDOW,
    advancing_service,
    lease_window,
    read_only_service,
    runtime_for_run,
    start_bound_run,
    watch_document,
)
from enginery.cli.stage1_request import RequestInputs, compile_request
from enginery.domain.errors import EngineryError, InvalidInputError
from enginery.domain.ids import RunId
from enginery.engine.operator_control import live_workers, read_control_state
from enginery.engine.runtime import RUN_AGGREGATE_TYPE, RUNTIME_NODE_AGGREGATE_TYPE
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import (
    Stage1ProgressionAction,
    Stage1RunService,
    first_attempt_generation,
    stage1_request_from_state,
)

_TERMINAL_STATUSES = frozenset({"succeeded", "cancelled", "failed", "rejected", "superseded"})
_EXECUTES_NOW = frozenset(
    {
        Stage1ProgressionAction.QUALIFY,
        Stage1ProgressionAction.IMPLEMENT,
        Stage1ProgressionAction.COLLECT_IMPLEMENTATION,
        Stage1ProgressionAction.VALIDATE,
        Stage1ProgressionAction.OPEN_PR,
        Stage1ProgressionAction.VERIFY,
        Stage1ProgressionAction.REGISTER_OUTCOME_OBSERVATION,
    }
)
_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0)
_TERMINAL_NODE_STATUSES = frozenset({"passed", "failed", "cancelled", "blocked"})


def nonnegative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--wait-seconds must be a finite non-negative number"
        ) from None
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("--wait-seconds must be a finite non-negative number")
    return seconds


def changed_hold(project: Path, issue: str, run_id: str) -> dict[str, str]:
    return {
        "prerequisite": "bound snapshot",
        "detail": f"issue changed since run {run_id} was bound",
        "resolve": (
            f"enginery stage1 cancel --database {shlex.quote(str(database_path(project)))} "
            f"--owner factory --run-id {shlex.quote(run_id)} --node-id implement; "
            f"enginery factory run {shlex.quote(issue)} --supersede "
            f"--project {shlex.quote(str(project))}"
        ),
    }


def _projection_status(project: Path, run_id: str) -> str | None:
    connection = read_ledger(project)
    if connection is None:
        return None
    try:
        projection = LedgerService(connection).read_projection(
            aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=run_id
        )
        return None if projection is None else str(projection.state["status"])
    finally:
        connection.close()


def _supersede_hold(project: Path, issue: str, run_id: str) -> dict[str, str] | None:
    connection = read_ledger(project)
    if connection is None:
        hold = changed_hold(project, issue, run_id)
        hold["detail"] += "; supersede cannot prove run.status: project ledger is absent"
        return hold
    try:
        ledger = LedgerService(connection)
        projection = ledger.read_projection(aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=run_id)
        run_status = None if projection is None else projection.state.get("status")
        if run_status in _TERMINAL_STATUSES:
            return None
        implementation = ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:implement"
        )
        publication = ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:open_pr"
        )
        implementation_state = {} if implementation is None else implementation.state
        publication_status = None if publication is None else publication.state.get("status")
        lease = ledger.read_lease(run_id=run_id, node_id="implement")
        expiry = (
            None
            if lease is None or lease.expires_at is None
            else datetime.fromisoformat(lease.expires_at)
        )
        lease_active = lease is not None and (
            expiry is None or expiry.tzinfo is None or expiry > datetime.now(UTC)
        )
        workers = [worker.attempt_id for worker in live_workers(ledger) if worker.run_id == run_id]
        if (
            implementation_state.get("status") in _TERMINAL_NODE_STATUSES
            and not lease_active
            and not workers
            and (publication is None or publication_status in _TERMINAL_NODE_STATUSES)
        ):
            return None
        hold = changed_hold(project, issue, run_id)
        hold["detail"] += (
            f"; supersede not safe: run.status={run_status!r}, "
            f"runtime_node[{run_id}:implement].status={implementation_state.get('status')!r}, "
            f"runtime_node[{run_id}:implement].attempt_id="
            f"{implementation_state.get('attempt_id')!r}, "
            f"node_leases[{run_id}:implement].attempt_id="
            f"{None if lease is None else lease.attempt_id!r}, "
            f"node_leases[{run_id}:implement].expires_at="
            f"{None if lease is None else lease.expires_at!r}, "
            f"live worker-supervisor attempts={workers!r}, "
            f"runtime_node[{run_id}:open_pr].status={publication_status!r}"
        )
        return hold
    finally:
        connection.close()


def request_path(project: Path, run_id: str) -> Path:
    # Run ids are identifiers, never paths supplied by the issue or operator.
    RunId(run_id)
    if "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise InvalidInputError("run identity must not contain path components")
    return private_state_directory(project) / "runs" / run_id / "request.json"


def _git(project: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(project), *arguments), capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise InvalidInputError(
            "git could not resolve the requested base branch; repair: git checkout BRANCH"
        )
    return result.stdout.strip()


def request_inputs(
    project: Path, issue: str, run_id: str, args: argparse.Namespace
) -> RequestInputs:
    branch = args.base_branch or _git(project, "symbolic-ref", "--short", "HEAD")
    return RequestInputs(
        output=request_path(project, run_id),
        run_id=run_id,
        repository=repository_name(project),
        external_reference=issue,
        repository_path=project,
        workspace_path=request_path(project, run_id).parent / "workspace",
        artifact_root=private_state_directory(project) / "artifacts",
        base_branch=branch,
        base_revision=_git(
            project, "rev-parse", "--verify", "--end-of-options", f"{branch}^{{commit}}"
        ),
        capability_lockfile=project / LOCKFILE_RELATIVE,
        model=args.model,
        effort=args.effort,
        runner=args.runner,
        profile=args.profile,
        validation_commands=args.validation_commands,
        required_checks=args.required_checks,
        protected_paths=args.protected_paths,
        implementation_time_budget_seconds=args.time_budget_seconds,
    )


def _held(project: Path, holds: list[dict[str, str]]) -> dict[str, object]:
    return {
        "project": str(project),
        "status": "hold",
        "holds": holds,
        "database": str(database_path(project)),
        "external_effects": {"github_mutations": 0, "hosted_inference_calls": 0},
    }


def run_issue(project: Path, args: argparse.Namespace) -> dict[str, object]:
    configuration = load_project_configuration(project)
    holds = intake_prerequisites(configuration)
    if holds:
        return _held(project, holds)
    try:
        repository = repository_name(project)
        snapshot = github_ledger(configuration, repository).fetch(args.issue)
    except EngineryError as error:
        return _held(
            project,
            [
                {
                    "prerequisite": "issue intake",
                    "detail": str(error),
                    "resolve": (
                        f"enginery factory collect --dry-run --project {shlex.quote(str(project))}"
                    ),
                }
            ],
        )
    issue = snapshot.work_item.external_reference
    digest = str(snapshot.bound_digest)
    request_rebuilt = False
    with intake_lock(project):
        index = read_index(project)
        intake = index.repositories.setdefault(repository, RepositoryIntake())
        previous = intake.issues.get(issue)
        if previous is not None:
            if previous.bound_digest != digest and not args.supersede:
                return _held(project, [changed_hold(project, issue, previous.run_id)])
            if args.supersede:
                hold = _supersede_hold(project, issue, previous.run_id)
                if hold is not None:
                    return _held(project, [hold])
        run_id = derive_run_id(project_identity(project), issue, digest)
        path = request_path(project, run_id)
        raw = read_document(path)
        bound = _projection_status(project, run_id) is not None
        if not bound:
            built = compile_request(request_inputs(project, issue, run_id, args))
            if str(built.request.work_snapshot.bound_digest) != digest:
                return _held(
                    project,
                    [
                        {
                            "prerequisite": "issue snapshot",
                            "detail": "issue changed during request compilation",
                            "resolve": (
                                f"enginery factory run {shlex.quote(issue)} "
                                f"--project {shlex.quote(str(project))}"
                            ),
                        }
                    ],
                )
            request_rebuilt = raw is not None and (
                not isinstance(raw, Mapping)
                or raw.get("request_digest") != str(built.request.digest)
            )
            if raw is None or request_rebuilt:
                raw = built.document()
                write_document(path, raw)
            request = stage1_request_from_state(raw)
            database = database_path(project)
            require_state_path(database, what="project ledger")
            ledger = LedgerService.open(database)
            try:
                start_bound_run(
                    ledger,
                    owner="factory",
                    request=request,
                    raw_request=cast(dict[str, object], raw),
                )
            finally:
                ledger.close()
        intake.issues[issue] = IntakeEntry(
            external_reference=issue,
            run_id=run_id,
            bound_digest=digest,
            source_revision=str(snapshot.source_revision),
            recorded_at=datetime.now(UTC).isoformat(),
        )
        write_document(index_path(project), index.to_state())
    report = advance_run(project, run_id, once=args.once, wait_seconds=args.wait_seconds)
    if request_rebuilt:
        report["request_rebuilt"] = True
    return report


def advance_run(
    project: Path, run_id: str, *, once: bool = False, wait_seconds: float = 0
) -> dict[str, object]:
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise InvalidInputError("--wait-seconds must be a finite non-negative number")
    if paused_hold(project):
        return status(project, run_id)
    require_state_path(database_path(project), what="project ledger")
    ledger = LedgerService.open(database_path(project))
    try:
        runtime = runtime_for_run(ledger, owner="factory", run_id=run_id)
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id))
        service = advancing_service(runtime=runtime, ledger=ledger, request=run.request)
        deadline = time.monotonic() + wait_seconds
        backoff = 0
        while True:
            previous = service.next_action(RunId(run_id))
            progression = service.advance(
                RunId(run_id),
                now=datetime.now(UTC),
                heartbeat_window=HEARTBEAT_WINDOW,
                lease_window=lease_window(previous.run.request),
                limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            )
            report = watch_document(
                ledger, runtime, progression, action_taken=previous.action.value
            )
            report = _enrich(project, ledger, report, run_id)
            if (
                once
                or progression.action.value in {"hold", "blocked", "await_human_review"}
                or progression.run.status.value in _TERMINAL_STATUSES
            ):
                return report
            unchanged = (
                progression.action == previous.action
                and progression.run.aggregate_version == previous.run.aggregate_version
            )
            if progression.action not in _EXECUTES_NOW or unchanged:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return report
                delay = _BACKOFF_SECONDS[backoff]
                if progression.action is Stage1ProgressionAction.WAIT_FOR_CI:
                    delay = max(15.0, delay)
                time.sleep(min(delay, remaining))
                if time.monotonic() >= deadline:
                    return report
                backoff = min(backoff + 1, len(_BACKOFF_SECONDS) - 1)
            else:
                backoff = 0
    finally:
        ledger.close()


def _enrich(
    project: Path, ledger: LedgerService, report: dict[str, object], run_id: str
) -> dict[str, object]:
    run = Stage1RunService(
        runtime=runtime_for_run(ledger, owner="factory", run_id=run_id), ledger=ledger
    ).read(RunId(run_id))
    request = run.request
    raw = read_document(request_path(project, run_id))
    snapshot = None if not isinstance(raw, Mapping) else raw.get("effective_config")
    fields: list[dict[str, object]] = []
    if snapshot is not None:
        config = EffectiveConfig.from_state(snapshot)
        fields = [{"name": field.name, **field.to_state()} for field in config.fields]
    workers = [
        node["worker"]
        for node in cast(list[dict[str, object]], report["nodes"])
        if node.get("worker") is not None
    ]
    hold = report.get("hold")
    if hold is None and report["next_action"] == "blocked":
        blocked = next(
            (
                node
                for node in cast(list[dict[str, object]], report["nodes"])
                if node["status"] == "blocked"
            ),
            None,
        )
        projection = (
            None
            if blocked is None
            else ledger.read_projection(
                aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
                aggregate_id=f"{run_id}:{blocked['node_id']}",
            )
        )
        state = {} if projection is None else projection.state
        hold = {
            "reason": state.get("reason", "blocked"),
            "message": state.get(
                "blocked_reason",
                state.get("message", report.get("reason", "Stage 1 progression is blocked")),
            ),
            **{key: state[key] for key in ("repair", "resolve", "details") if key in state},
        }
    holds = [] if not isinstance(hold, Mapping) else [_hold_with_repair(project, hold)]
    if report["next_action"] == "await_human_review":
        attempt = next(
            (
                node["attempt_id"]
                for node in cast(list[dict[str, object]], report["nodes"])
                if node["node_id"] == "implement"
            ),
            None,
        )
        repair_attempt = (
            first_attempt_generation(attempt)
            - first_attempt_generation(request.implementation.attempt_id)
            if isinstance(attempt, str)
            else 0
        )
        report["next_decision"] = {
            "action": "stage1 review",
            "command": (
                f"enginery stage1 review --database {shlex.quote(str(database_path(project)))} "
                f"--owner factory --run-id {shlex.quote(run_id)} --report REPORT.json "
                f"--repair-attempt {repair_attempt}"
            ),
        }
    report.update(
        {
            "database": str(database_path(project)),
            "request_path": str(request_path(project, run_id)),
            "issue": request.work_snapshot.work_item.external_reference,
            "worker": workers[0] if workers else None,
            "current_node": next(
                (
                    node["node_id"]
                    for node in cast(list[dict[str, object]], report["nodes"])
                    if node["status"] == "running"
                ),
                report["next_action"],
            ),
            "selection": request.execution_selection.to_state(),
            "runner": request.runner.to_state(),
            "fields": fields,
            "request_digest": str(request.digest),
            "configuration_snapshot_digest": str(request.run.configuration_snapshot_digest),
            "permitted_capabilities": list(request.implementation.permitted_capabilities),
            "budget": {
                "time_budget_seconds": request.implementation.time_budget_seconds,
                "cost": "unenforced",
            },
            "paused": read_control_state(ledger).paused,
            "holds": holds,
        }
    )
    return report


def _hold_with_repair(project: Path, hold: Mapping[str, object]) -> dict[str, object]:
    details = hold.get("details")
    repair = hold.get("resolve") or hold.get("repair")
    if not repair and isinstance(details, Mapping):
        repair = details.get("resolve") or details.get("repair")
    if not repair:
        reason = hold.get("reason")
        if reason == "worker_identity_unprovable":
            repair = "Resolve the worker on its launching host"
        else:
            doctor = f"enginery factory doctor --project {shlex.quote(str(project))}"
            message = (
                "restore the run's bound configuration"
                if reason
                in {
                    "configuration_drift",
                    "catalog_drift",
                    "host_fingerprint_drift",
                    "runner_unqualified",
                    "runner_fingerprint_drift",
                }
                else str(hold.get("message", reason or "resolve the reported hold"))
            )
            repair = f"{doctor}; {message}"
    return {**hold, "resolve": repair}


def status(project: Path, run_id: str | None = None) -> dict[str, object]:
    path = database_path(project)
    connection = read_ledger(project)
    if connection is None:
        if run_id is not None:
            raise InvalidInputError(f"unknown factory run: {run_id}")
        return {"database": str(path), "runs": [], "paused": False}
    ledger = LedgerService(connection)
    try:
        if run_id is None:
            identities = [
                projection.aggregate_id
                for projection in ledger.list_projections(aggregate_type=RUN_AGGREGATE_TYPE)
            ]
            return {
                "database": str(path),
                "paused": read_control_state(ledger).paused,
                "runs": [status(project, identity) for identity in identities],
            }
        runtime = runtime_for_run(ledger, owner="factory", run_id=run_id)
        try:
            run = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id))
        except EngineryError as error:
            raise InvalidInputError(f"unknown factory run: {run_id}") from error
        service = read_only_service(runtime=runtime, ledger=ledger, request=run.request)
        progression = service.next_action(RunId(run_id))
        report = watch_document(ledger, runtime, progression)
        report = _enrich(project, ledger, report, run_id)
        report["holds"] = [*cast(list[object], report["holds"]), *paused_hold(project)]
        return report
    finally:
        ledger.close()
