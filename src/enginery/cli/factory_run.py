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
from enginery.application.human_decisions import decision_commands
from enginery.cli.config_locations import private_state_directory, project_identity
from enginery.cli.factory_collect import (
    database_path,
    github_ledger,
    index_path,
    intake_lock,
    intake_prerequisites,
    ledger_sidecars,
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
    nodes,
    read_only_service,
    runtime_for_run,
    start_bound_run,
    watch_document,
)
from enginery.cli.stage1_request import RequestInputs, compile_request
from enginery.domain.errors import EngineryError, InvalidInputError
from enginery.domain.ids import NodeId, RunId
from enginery.domain.outcome import OutcomeKind
from enginery.domain.run import RUN_TRANSITIONS
from enginery.domain.serialization import observation_to_dict
from enginery.domain.workflow.node import ActorType
from enginery.engine.operator_control import live_workers, read_control_state
from enginery.engine.runtime import (
    RUN_AGGREGATE_TYPE,
    RUNTIME_NODE_AGGREGATE_TYPE,
    CoordinatorRuntime,
)
from enginery.engine.scheduler import SchedulingLimits
from enginery.evaluation.outcomes import OutcomeCaptureService, observation_id_for
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import (
    Stage1ProgressionAction,
    Stage1RunRequest,
    Stage1RunService,
    first_attempt_generation,
    stage1_request_from_state,
)

_TERMINAL_STATUSES = frozenset(state.value for state in RUN_TRANSITIONS.terminal_states)
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
    repairs = [f"enginery factory drain --stop --project {shlex.quote(str(project))}"]
    connection = read_ledger(project)
    if connection is not None:
        try:
            ledger = LedgerService(connection)
            runtime = runtime_for_run(ledger, owner="factory", run_id=run_id)
            request = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id)).request
            live_node_ids = {
                worker.node_id for worker in live_workers(ledger) if worker.run_id == run_id
            }
            for node in ledger.list_projections(aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE):
                node_id = node.aggregate_id.removeprefix(f"{run_id}:")
                if (
                    node.aggregate_id.startswith(f"{run_id}:")
                    and node.state.get("status") in {"queued", "running"}
                    and request.manifest.nodes[NodeId(node_id)].actor_type is ActorType.AGENT
                    and node_id not in live_node_ids
                ):
                    repairs.append(
                        f"enginery stage1 cancel "
                        f"--database {shlex.quote(str(database_path(project)))} "
                        f"--owner factory --run-id {shlex.quote(run_id)} "
                        f"--node-id {shlex.quote(node_id)}"
                    )
        finally:
            connection.close()
    repairs.append(f"enginery factory pause --lift --project {shlex.quote(str(project))}")
    repairs.append(
        f"enginery factory run {shlex.quote(issue)} --supersede "
        f"--project {shlex.quote(str(project))}"
    )
    return {
        "prerequisite": "bound snapshot",
        "detail": f"issue changed since run {run_id} was bound",
        "resolve": "; ".join(repairs),
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
        active_leases = []
        for node_id, expires_at in connection.execute(
            "SELECT node_id, expires_at FROM node_leases WHERE run_id = ?", (run_id,)
        ):
            expiry = None if expires_at is None else datetime.fromisoformat(expires_at)
            if expiry is None or expiry.tzinfo is None or expiry > datetime.now(UTC):
                active_leases.append(node_id)
        workers = [worker.attempt_id for worker in live_workers(ledger) if worker.run_id == run_id]
        if (
            (
                implementation_state.get("status") in _TERMINAL_NODE_STATUSES
                or (implementation is None and publication is None)
            )
            and not active_leases
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
            f"active node leases={active_leases!r}, "
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
    holds = factory_repairs(project, intake_prerequisites(configuration))
    if holds:
        return _held(project, holds)
    try:
        repository = repository_name(project)
        snapshot = github_ledger(configuration, repository).fetch(args.issue)
    except EngineryError as error:
        compiled = configuration.compiled
        executable = "gh" if compiled is None else compiled.config.text("github_executable")
        return _held(
            project,
            [
                {
                    "prerequisite": "issue intake",
                    "detail": str(error),
                    "resolve": (
                        f"{shlex.quote(executable)} auth status; "
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
            if previous.bound_digest == digest and args.supersede:
                raise InvalidInputError(
                    f"issue snapshot unchanged since run {previous.run_id} was bound; "
                    f"use enginery factory run {shlex.quote(issue)} "
                    f"--project {shlex.quote(str(project))}"
                )
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
            if not built.request.behavioral_checks:
                branch = shlex.quote(built.request.base_branch)
                return _held(
                    project,
                    [
                        {
                            "prerequisite": "behavioral checks",
                            "reason": "no_behavioral_checks",
                            "detail": "no behavioral checks were bound; nothing was started",
                            "resolve": (
                                f"add [acceptance] checks to .enginery/project.toml, commit on "
                                f"{branch}, then enginery factory run {shlex.quote(issue)} "
                                f"--project {shlex.quote(str(project))}"
                            ),
                        }
                    ],
                )
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


def resume_run(project: Path, run_id: str, wait_seconds: float = 0) -> dict[str, object]:
    """Resume the durable run without compiling another request."""
    status(project, run_id)
    return advance_run(project, run_id, wait_seconds=wait_seconds)


def advance_run(
    project: Path, run_id: str, *, once: bool = False, wait_seconds: float = 0
) -> dict[str, object]:
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise InvalidInputError("--wait-seconds must be a finite non-negative number")
    if paused_hold(project) or status(project, run_id)["superseded_by"] is not None:
        return status(project, run_id)
    require_state_path(database_path(project), what="project ledger")
    ledger = LedgerService.open(database_path(project))
    try:
        runtime = runtime_for_run(ledger, owner="factory", run_id=run_id)
        run = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id))
        service = advancing_service(runtime=runtime, ledger=ledger, request=run.request)
        deadline = time.monotonic() + wait_seconds
        backoff = 0
        progression = service.next_action(RunId(run_id))
        while True:
            before = _node_digest(nodes(ledger, runtime, run_id=run_id))
            report = _enrich(project, ledger, watch_document(ledger, runtime, progression), run_id)
            if _finished(report):
                return report
            previous = progression
            if _superseded_by(project, run.request, run_id) is not None:
                return status(project, run_id)
            progression = service.advance(
                RunId(run_id),
                now=datetime.now(UTC),
                heartbeat_window=HEARTBEAT_WINDOW,
                lease_window=lease_window(previous.run.request),
                limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            )
            last_advance = time.monotonic()
            report = _enrich(
                project,
                ledger,
                watch_document(ledger, runtime, progression, action_taken=previous.action.value),
                run_id,
            )
            if once or _finished(report):
                return report
            digest = _node_digest(cast(list[dict[str, object]], report["nodes"]))
            if progression.action in _EXECUTES_NOW and (
                progression.action != previous.action or digest != before
            ):
                backoff = 0
                continue
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return report
                delay = _BACKOFF_SECONDS[backoff]
                if progression.action is Stage1ProgressionAction.WAIT_FOR_CI:
                    delay = max(15.0, delay)
                time.sleep(min(delay, remaining))
                if _superseded_by(project, run.request, run_id) is not None:
                    return status(project, run_id)
                observed = service.next_action(RunId(run_id))
                current_nodes = nodes(ledger, runtime, run_id=run_id)
                current_digest = _node_digest(current_nodes)
                report = _enrich(project, ledger, watch_document(ledger, runtime, observed), run_id)
                if _finished(report) or time.monotonic() >= deadline:
                    return report
                backoff = min(backoff + 1, len(_BACKOFF_SECONDS) - 1)
                if (
                    observed.action != progression.action
                    or current_digest != digest
                    or (
                        (
                            observed.action in _EXECUTES_NOW
                            or observed.action.value
                            in {"dispatch_queued", "reconcile_worker", "wait_for_ci", "wait"}
                        )
                        and time.monotonic() - last_advance >= 15.0
                    )
                ):
                    if observed.action != progression.action or current_digest != digest:
                        backoff = 0
                    progression = observed
                    break
    finally:
        ledger.close()


def _node_digest(node_states: list[dict[str, object]]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            str(node["node_id"]),
            str(node.get("status")),
            str(node.get("attempt_id")),
            str(worker.get("liveness") if isinstance(worker, Mapping) else None),
        )
        for node in node_states
        for worker in (node.get("worker"),)
    )


def _finished(report: Mapping[str, object]) -> bool:
    node_states = cast(list[dict[str, object]], report["nodes"])
    return bool(
        report.get("holds")
        or report.get("workflow_complete")
        or report["next_action"] in {"hold", "blocked", "await_human_review"}
        or report["status"] in _TERMINAL_STATUSES
        or any(node["status"] in {"awaiting_human", "blocked"} for node in node_states)
        or (
            report["next_action"] == "wait"
            and node_states
            and all(node["status"] in _TERMINAL_NODE_STATUSES for node in node_states)
        )
    )


def factory_repairs(project: Path, holds: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            **hold,
            "resolve": f"enginery factory pause --lift --project {shlex.quote(str(project))}",
        }
        if hold.get("prerequisite") == "dispatch"
        else hold
        for hold in holds
    ]


def workspace_documents(project: Path, ledger: LedgerService) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for reservation in CoordinatorRuntime(ledger, owner="factory").list_workspace_reservations():
        command = shlex.join(
            [
                "enginery",
                "workspace",
                "release",
                "--database",
                str(database_path(project)),
                "--owner",
                "factory",
                "--repository-id",
                reservation.repository_id,
                "--run-id",
                reservation.run_id,
                "--json",
            ]
        )
        rows.append(
            {
                "status": reservation.status,
                "run_id": reservation.run_id,
                "repository_id": reservation.repository_id,
                "path": str(reservation.workspace_path),
                "resolve": command if reservation.status == "retained" else None,
            }
        )
    return rows


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
        if config.digest != request.run.configuration_snapshot_digest:
            raise InvalidInputError(
                "the retained request snapshot differs from the durable binding"
            )
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
    node_states = cast(list[dict[str, object]], report["nodes"])
    awaiting = next((node for node in node_states if node["status"] == "awaiting_human"), None)
    prefix = (
        f"--database {shlex.quote(str(database_path(project)))} "
        f"--owner factory --run-id {shlex.quote(run_id)}"
    )
    if awaiting is not None and awaiting["node_id"] != "review":
        node_id = str(awaiting["node_id"])
        action = "pull_request.open" if node_id == "open_pr" else f"human_wait.{node_id}.approve"
        review_command, decide_command = decision_commands(run_id, str(project), action)
        _, reject_command = decision_commands(run_id, str(project), f"human_wait.{node_id}.reject")
        report["next_decision"] = {
            "action": "factory review|decide",
            "review_command": review_command,
            "command": decide_command,
            "reject_command": reject_command,
        }
    elif report["next_action"] == "await_human_review":
        implementation = next(
            (node for node in node_states if node["node_id"] == "implement"), None
        )
        validation = ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:validate"
        )
        review_ready = (
            implementation is not None
            and implementation["status"] == "passed"
            and validation is not None
            and validation.state.get("status") == "passed"
            and validation.state.get("validation_artifact_digest") is not None
            and first_attempt_generation(str(validation.state["attempt_id"]))
            == first_attempt_generation(str(implementation["attempt_id"]))
        )
        if (awaiting is not None and awaiting["node_id"] == "review") or review_ready:
            attempt = None if implementation is None else implementation["attempt_id"]
            repair_attempt = (
                first_attempt_generation(attempt)
                - first_attempt_generation(request.implementation.attempt_id)
                if isinstance(attempt, str)
                else 0
            )
            report["next_decision"] = {
                "action": "stage1 review",
                "command": (
                    f"enginery stage1 review {prefix} --report REPORT.json "
                    f"--repair-attempt {repair_attempt}"
                ),
            }
        elif hold is None:
            failed = next(
                (
                    node
                    for node in node_states
                    if node["status"] in _TERMINAL_NODE_STATUSES and node["status"] != "passed"
                ),
                None,
            )
            projection = (
                None
                if failed is None
                else ledger.read_projection(
                    aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
                    aggregate_id=f"{run_id}:{failed['node_id']}",
                )
            )
            state = {} if projection is None else projection.state
            failure_message = (
                "Stage 1 requires a human decision"
                if failed is None
                else f"Stage 1 node {failed['node_id']} is {failed['status']}"
            )
            hold = {
                "reason": state.get("reason", "human_action_required"),
                "message": state.get("message", report.get("reason", failure_message)),
                **{key: state[key] for key in ("repair", "resolve", "details") if key in state},
            }
            details = hold.get("details")
            supplied_repair = hold.get("repair") or hold.get("resolve")
            if isinstance(details, Mapping):
                supplied_repair = supplied_repair or details.get("repair") or details.get("resolve")
            if not supplied_repair:
                hold["resolve"] = (
                    f"enginery factory resume {shlex.quote(run_id)} "
                    f"--project {shlex.quote(str(project))}"
                )
    holds = [] if not isinstance(hold, Mapping) else [_hold_with_repair(project, hold, run_id)]
    node_index = {str(node["node_id"]): node for node in node_states}
    graph = [
        {
            "node_id": str(node.node_id),
            "kind": node.kind.value,
            "actor": node.actor_type.value,
            "dependencies": [str(dependency) for dependency in node.dependencies],
            "status": node_index.get(str(node.node_id), {}).get("status", "not_started"),
        }
        for node in request.manifest.nodes.values()
    ]
    opened = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:open_pr"
    )
    pull_request = (
        None
        if opened is None
        else {
            "number": opened.state.get("pull_request_number"),
            "head_revision": opened.state.get("head_revision"),
            "base_revision": opened.state.get("base_revision"),
            "head_branch": request.head_branch,
            "operation_id": opened.state.get("operation_id"),
        }
    )
    verified = ledger.read_projection(
        aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:verify"
    )
    decision = (
        verified.state.get("pull_request_outcome")
        if verified is not None and verified.state.get("status") == "passed"
        else None
    )
    observation_id = observation_id_for(RunId(run_id), OutcomeKind.MERGE_RESULT)
    observation = OutcomeCaptureService(ledger=ledger).read_observation(observation_id)
    workspaces = workspace_documents(project, ledger)
    owned_workspace = next((row for row in workspaces if row["run_id"] == run_id), None)
    blocked_workspace = next(
        (
            row
            for row in workspaces
            if row["repository_id"] == request.repository_id
            and row["run_id"] != run_id
            and row["status"] == "retained"
        ),
        None,
    )
    if blocked_workspace is not None and report["next_action"] in {"implement", "dispatch_queued"}:
        holds.append(
            {
                "prerequisite": "retained workspace",
                "detail": f"repository workspace is retained by run {blocked_workspace['run_id']}",
                "resolve": blocked_workspace["resolve"],
            }
        )
    report.update(
        {
            "database": str(database_path(project)),
            "request_path": str(request_path(project, run_id)),
            "issue": request.work_snapshot.work_item.external_reference,
            "worker": workers[0] if workers else None,
            "workers": workers,
            "workspace": owned_workspace,
            "blocked_workspace": blocked_workspace,
            "node_graph": graph,
            "terminal_decisions": sorted(request.manifest.terminal_states),
            "pull_request": pull_request,
            "terminal_decision": decision,
            "outcome_observation": (
                None if observation is None else observation_to_dict(observation)
            ),
            "workflow_complete": (
                decision == "merge_ready"
                and observation is not None
                and report["next_action"] == "wait"
            ),
            "terminal_evidence": {
                "verify_projection": f"runtime_node:{run_id}:verify",
                "verify_status": None if verified is None else verified.state.get("status"),
                "pull_request_outcome": decision,
                "observation_id": str(observation_id),
                "observation_registered": observation is not None,
                "next_action": report["next_action"],
            },
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


def _hold_with_repair(project: Path, hold: Mapping[str, object], run_id: str) -> dict[str, object]:
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
            if message == "restore the run's bound configuration":
                repair += (
                    f"; enginery factory resume {shlex.quote(run_id)} "
                    f"--project {shlex.quote(str(project))}"
                )
    return {**hold, "resolve": repair}


def _superseded_by(project: Path, request: Stage1RunRequest, run_id: str) -> str | None:
    intake = read_index(project).repositories.get(str(request.repository_id))
    binding = (
        None
        if intake is None
        else intake.issues.get(request.work_snapshot.work_item.external_reference)
    )
    return binding.run_id if binding is not None and binding.run_id != run_id else None


def _created_sidecars(project: Path, existing: frozenset[Path]) -> list[str]:
    created = ledger_sidecars(project) - existing
    return [str(path) for path in sorted(created, key=lambda path: not path.name.endswith("-wal"))]


def status(project: Path, run_id: str | None = None) -> dict[str, object]:
    path = database_path(project)
    existing_sidecars = ledger_sidecars(project)
    connection = read_ledger(project)
    if connection is None:
        if run_id is not None:
            raise InvalidInputError(f"unknown factory run: {run_id}")
        return {"database": str(path), "runs": [], "paused": False, "local_writes": []}
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
                "local_writes": _created_sidecars(project, existing_sidecars),
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
        report["holds"] = [
            *cast(list[object], report["holds"]),
            *factory_repairs(project, paused_hold(project)),
        ]
        superseded_by = _superseded_by(project, run.request, run_id)
        report["superseded_by"] = superseded_by
        if superseded_by is not None:
            cast(list[object], report["holds"]).append(
                {
                    "prerequisite": "superseded binding",
                    "detail": f"superseded by run {superseded_by}",
                    "resolve": (
                        f"enginery factory status {shlex.quote(superseded_by)} "
                        f"--project {shlex.quote(str(project))}"
                    ),
                }
            )
        report["local_writes"] = _created_sidecars(project, existing_sidecars)
        return report
    finally:
        ledger.close()
