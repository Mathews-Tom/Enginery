"""Manual factory commands composed over the authoritative Stage 1 lifecycle."""

from __future__ import annotations

import argparse
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
    changed_hold,
    github_ledger,
    index_path,
    intake_lock,
    intake_prerequisites,
    paused_hold,
    read_document,
    read_index,
    repository_name,
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
from enginery.engine.operator_control import read_control_state
from enginery.engine.runtime import RUN_AGGREGATE_TYPE
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1RunService


def database_path(project: Path) -> Path:
    return private_state_directory(project) / "ledger.sqlite"


def request_path(project: Path, run_id: str) -> Path:
    # Run ids are identifiers, never paths supplied by the issue or operator.
    RunId(run_id)
    if "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise InvalidInputError("run identity must not contain path components")
    return private_state_directory(project) / "runs" / run_id / "request.json"


def _git(project: Path, *arguments: str) -> str:
    result = subprocess.run(("git", "-C", str(project), *arguments),
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise InvalidInputError("git could not resolve the requested base branch; repair: git checkout BRANCH")
    return result.stdout.strip()


def request_inputs(project: Path, issue: str, run_id: str, args: argparse.Namespace) -> RequestInputs:
    branch = args.base_branch or _git(project, "symbolic-ref", "--short", "HEAD")
    return RequestInputs(
        output=request_path(project, run_id), run_id=run_id, repository=repository_name(project),
        external_reference=issue, repository_path=project,
        workspace_path=request_path(project, run_id).parent / "workspace",
        artifact_root=private_state_directory(project) / "artifacts",
        base_branch=branch, base_revision=_git(project, "rev-parse", f"{branch}^{{commit}}"),
        capability_lockfile=project / LOCKFILE_RELATIVE,
        model=args.model, effort=args.effort, runner=args.runner, profile=args.profile,
        validation_commands=args.validation_commands, required_checks=args.required_checks,
        protected_paths=args.protected_paths,
        implementation_time_budget_seconds=args.time_budget_seconds,
    )


def _held(project: Path, holds: list[dict[str, str]]) -> dict[str, object]:
    return {"project": str(project), "status": "hold", "holds": holds,
            "database": str(database_path(project)),
            "external_effects": {"github_mutations": 0, "hosted_inference_calls": 0}}


def run_issue(project: Path, args: argparse.Namespace) -> dict[str, object]:
    configuration = load_project_configuration(project)
    holds = intake_prerequisites(configuration)
    if holds:
        return _held(project, holds)
    try:
        repository = repository_name(project)
        snapshot = github_ledger(configuration, repository).fetch(args.issue)
    except EngineryError as error:
        return _held(project, [{"prerequisite": "issue intake", "detail": str(error),
                                "resolve": f"enginery factory collect --dry-run --project {project}"}])
    digest = str(snapshot.bound_digest)
    with intake_lock(project):
        index = read_index(project)
        intake = index.repositories.setdefault(repository, RepositoryIntake())
        previous = intake.issues.get(args.issue)
        if previous is not None and previous.bound_digest != digest and not args.supersede:
            return _held(project, [changed_hold(project, args.issue, previous.run_id)])
        run_id = derive_run_id(project_identity(project), args.issue, digest)
        path = request_path(project, run_id)
        raw = read_document(path)
        if raw is None:
            built = compile_request(request_inputs(project, args.issue, run_id, args))
            if str(built.request.work_snapshot.bound_digest) != digest:
                return _held(project, [{"prerequisite": "issue snapshot",
                    "detail": "issue changed during request compilation",
                    "resolve": f"enginery factory run {args.issue} --project {project}"}])
            raw = built.document()
            write_document(path, raw)
        ledger = LedgerService.open(database_path(project))
        try:
            start_bound_run(ledger, owner="factory", raw_request=cast(dict[str, object], raw))
        finally:
            ledger.close()
        intake.issues[args.issue] = IntakeEntry(
            external_reference=args.issue, run_id=run_id, bound_digest=digest,
            source_revision=str(snapshot.source_revision), recorded_at=datetime.now(UTC).isoformat(),
        )
        write_document(index_path(project), index.to_state())
    return advance_run(project, run_id, once=args.once, wait_seconds=args.wait_seconds)


def advance_run(project: Path, run_id: str, *, once: bool = False,
                wait_seconds: float = 0) -> dict[str, object]:
    if wait_seconds < 0:
        raise InvalidInputError("--wait-seconds must be non-negative")
    holds = paused_hold(project)
    if holds:
        report = status(project, run_id)
        report["holds"] = holds
        return report
    ledger = LedgerService.open(database_path(project))
    try:
        runtime = runtime_for_run(ledger, owner="factory", run_id=run_id)
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id))
        service = advancing_service(runtime=runtime, ledger=ledger, request=run.request)
        deadline = time.monotonic() + wait_seconds
        while True:
            previous = service.next_action(RunId(run_id))
            progression = service.advance(
                RunId(run_id), now=datetime.now(UTC), heartbeat_window=HEARTBEAT_WINDOW,
                lease_window=lease_window(previous.run.request),
                limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            )
            report = watch_document(ledger, runtime, progression, action_taken=previous.action.value)
            report = _enrich(project, ledger, report, run_id)
            if (once or progression.action.value in {"hold", "blocked", "await_human_review"}
                    or progression.run.status.value in {"succeeded", "cancelled", "failed", "rejected", "superseded"}):
                return report
            if progression.action.value in {"wait", "wait_for_ci", "reconcile_worker"}:
                if time.monotonic() >= deadline:
                    return report
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    finally:
        ledger.close()


def _enrich(project: Path, ledger: LedgerService, report: dict[str, object], run_id: str) -> dict[str, object]:
    run = Stage1RunService(runtime=runtime_for_run(ledger, owner="factory", run_id=run_id), ledger=ledger).read(RunId(run_id))
    request = run.request
    raw = read_document(request_path(project, run_id))
    snapshot = None if not isinstance(raw, Mapping) else raw.get("effective_config")
    fields: list[dict[str, object]] = []
    if snapshot is not None:
        config = EffectiveConfig.from_state(snapshot)
        fields = [{"name": field.name, **field.to_state()} for field in config.fields]
    workers = [node["worker"] for node in cast(list[dict[str, object]], report["nodes"])
               if node.get("worker") is not None]
    holds = [] if report["hold"] is None else [
        {**cast(dict[str, object], report["hold"]),
         "resolve": f"enginery factory doctor --project {project}; restore the run's bound configuration"}
    ]
    report.update({
        "database": str(database_path(project)), "request_path": str(request_path(project, run_id)),
        "issue": request.work_snapshot.work_item.external_reference,
        "worker": workers[0] if workers else None,
        "current_node": next((node["node_id"] for node in cast(list[dict[str, object]], report["nodes"])
                              if node["status"] == "running"), report["next_action"]),
        "selection": request.execution_selection.to_state(), "runner": request.runner.to_state(),
        "fields": fields, "request_digest": str(request.digest),
        "configuration_snapshot_digest": str(request.run.configuration_snapshot_digest),
        "permitted_capabilities": list(request.implementation.permitted_capabilities),
        "budget": {"time_budget_seconds": request.implementation.time_budget_seconds, "cost": "unenforced"},
        "paused": read_control_state(ledger).paused, "holds": holds,
    })
    return report


def status(project: Path, run_id: str | None = None) -> dict[str, object]:
    path = database_path(project)
    if not path.exists():
        if run_id is not None:
            raise InvalidInputError(f"unknown factory run: {run_id}")
        return {"database": str(path), "runs": [], "paused": False}
    ledger = LedgerService.open(path)
    try:
        if run_id is None:
            identities = [projection.aggregate_id for projection in ledger.list_projections(
                aggregate_type=RUN_AGGREGATE_TYPE)]
            return {"database": str(path), "paused": read_control_state(ledger).paused,
                    "runs": [status(project, identity) for identity in identities]}
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
