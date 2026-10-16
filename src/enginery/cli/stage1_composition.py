"""Shared composition for the authoritative Stage 1 lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.claude_code import probe_installed_host as probe_installed_claude
from enginery.adapters.git_publication import GitBranchPublisher
from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubPullRequests,
    GitHubWorkLedger,
)
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.adapters.omp import probe_installed_host as probe_installed_omp
from enginery.cli.config_binding import bind_behavioral_checks, require_bound_configuration
from enginery.cli.factory_behavioral import verify_for_stage1
from enginery.cli.stage1_execution import execution_report
from enginery.cli.stage1_runner import (
    DriftedRunnerWiring,
    IsolatedWiring,
    container_gone_for,
    isolated_wiring,
)
from enginery.domain.errors import InternalInvariantViolationError, InvalidInputError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.ids import RunId
from enginery.engine.recovery import assess_worker
from enginery.engine.runtime import (
    RUN_AGGREGATE_TYPE,
    RUNTIME_NODE_AGGREGATE_TYPE,
    CoordinatorRuntime,
)
from enginery.engine.task_repository import intake_path_for
from enginery.evaluation.outcomes import OutcomeCaptureService
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows.implementation import SupervisedHarness
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1Progression,
    Stage1Run,
    Stage1RunRequest,
    Stage1RunService,
)

HEARTBEAT_WINDOW = timedelta(seconds=60)


def lease_window(request: Stage1RunRequest) -> timedelta:
    """Cover one OMP time budget plus collection after the process exits."""
    return timedelta(
        seconds=request.implementation.time_budget_seconds + int(HEARTBEAT_WINDOW.total_seconds())
    )


def runtime_for_run(ledger: LedgerService, *, owner: str, run_id: str) -> CoordinatorRuntime:
    """A runtime whose recovery can prove an isolated run's runner container is gone.

    Recovery of a task-local worker must see that the launcher and its container are both
    absent before it records the worker as exited. The container lookup needs the run's bound
    engine, so the run is read first; a run that is not a Stage 1 run gets the plain runtime.
    """
    plain = CoordinatorRuntime(ledger, owner=owner)
    try:
        request = Stage1RunService(runtime=plain, ledger=ledger).read(RunId(run_id)).request
    except InternalInvariantViolationError:
        return plain
    container_gone = container_gone_for(request.runner)
    if container_gone is None:
        return plain
    return CoordinatorRuntime(ledger, owner=owner, container_gone=container_gone)


def harness_for(
    configuration: Stage1ExecutionConfiguration, wiring: IsolatedWiring | None = None
) -> SupervisedHarness:
    artifact_store = ArtifactStore(configuration.artifact_root)
    if configuration.harness_provider == "omp":
        omp_config = OmpAdapterConfig(
            credential_reference=configuration.harness_credential_reference,
            executable=configuration.harness_executable,
        )
        if wiring is None:
            return OmpHarness(omp_config, artifact_store)
        return OmpHarness(
            omp_config, artifact_store, command_runner=wiring.probe, runner=wiring.context
        )
    if configuration.harness_provider == "claude-code":
        claude_config = ClaudeCodeAdapterConfig(
            credential_reference=configuration.harness_credential_reference,
            executable=configuration.harness_executable,
        )
        if wiring is None:
            return ClaudeCodeHarness(claude_config, artifact_store)
        return ClaudeCodeHarness(
            claude_config, artifact_store, command_runner=wiring.probe, runner=wiring.context
        )
    raise InvalidInputError(
        "Stage 1 harness_provider is not a configured harness",
        details={"harness_provider": configuration.harness_provider},
    )


def once(probe: Callable[[], InstalledHost | None]) -> Callable[[], InstalledHost | None]:
    """Run ``probe`` at most once: the pinned image is immutable, so one answer holds for a run."""
    answers: list[InstalledHost | None] = []

    def probed() -> InstalledHost | None:
        if not answers:
            answers.append(probe())
        return answers[0]

    return probed


def host_probe_for(
    configuration: Stage1ExecutionConfiguration, wiring: IsolatedWiring | None
) -> Callable[[], InstalledHost | None]:
    """Return a probe that only runs the identity command of the harness that will run.

    A trusted-local run probes the host's own binary. An isolated run probes the binary in the
    pinned image, in a throw-away hardened container: the host binary is not what runs. No
    artifact store and no files are created either way.
    """
    if configuration.harness_provider == "omp":
        if wiring is None:
            return lambda: probe_installed_omp(configuration.harness_executable)
        return once(lambda: probe_installed_omp(configuration.harness_executable, wiring.probe))
    if configuration.harness_provider == "claude-code":
        if wiring is None:
            return lambda: probe_installed_claude(configuration.harness_executable)
        return once(lambda: probe_installed_claude(configuration.harness_executable, wiring.probe))
    raise InvalidInputError(
        "Stage 1 harness_provider is not a configured harness",
        details={"harness_provider": configuration.harness_provider},
    )


def read_only_service(
    *, runtime: CoordinatorRuntime, ledger: LedgerService, request: Stage1RunRequest
) -> Stage1RunService:
    """A service for diagnostics and decisions that must never create harness state."""
    wiring = isolated_wiring(request, create_directories=False)
    if isinstance(wiring, DriftedRunnerWiring):
        return Stage1RunService(runtime=runtime, ledger=ledger, runner_admission=wiring.admission)
    return Stage1RunService(
        runtime=runtime,
        ledger=ledger,
        host_probe=host_probe_for(request.execution_configuration, wiring),
        runner_admission=None if wiring is None else wiring.admission,
    )


def advancing_service(
    *, runtime: CoordinatorRuntime, ledger: LedgerService, request: Stage1RunRequest
) -> Stage1RunService:
    configuration = request.execution_configuration
    wiring = isolated_wiring(request, create_directories=True)
    github = GitHubAdapterConfig(
        repository=configuration.github_repository,
        credential_reference=configuration.github_credential_reference,
        executable=configuration.github_executable,
    )
    pull_requests = GitHubPullRequests(github)
    # A run bound to a profile that is no longer current gets no harness and no validation
    # runner: it can only report its hold, and nothing can launch under other restrictions.
    harness = (
        None if isinstance(wiring, DriftedRunnerWiring) else harness_for(configuration, wiring)
    )
    isolated_validation = wiring.validation if isinstance(wiring, IsolatedWiring) else None
    return Stage1RunService(
        runtime=runtime,
        ledger=ledger,
        work_ledger=GitHubWorkLedger(github),
        pull_requests=pull_requests,
        harness=harness,
        outcomes=OutcomeCaptureService(ledger=ledger, pull_requests=pull_requests),
        runner_admission=None if wiring is None else wiring.admission,
        isolated_command_runner=isolated_validation,
        branch_publisher=branch_publisher(request),
        behavioral_verifier=lambda bound, head: verify_for_stage1(ledger, bound, head),
    )


def branch_publisher(request: Stage1RunRequest) -> GitBranchPublisher | None:
    """Return the host-side publisher of a new run's verified branch.

    A new run's worker commits locally and holds no remote or credential. Its verified branch is
    pushed from a repository the worker cannot write to (the host-owned intake repository of a
    task-local workspace; the workspace of a trusted-local run) to the host repository's own
    ``origin``. A run persisted before trust modes has its worker push, so it has no publisher.
    """
    if request.runner.legacy:
        return None
    source = (
        intake_path_for(request.workspace_path)
        if request.runner.isolated_mode
        else request.workspace_path
    )
    return GitBranchPublisher(repository_path=request.repository_path, source_path=source)


def cost_budget_report(request: Stage1RunRequest) -> dict[str, object]:
    """Report a carried hard-dollar number as unenforced, so it is never read as a limit."""
    value = request.implementation.cost_budget
    return {"value": None if value is None else str(value), "enforced": False}


def nodes(
    ledger: LedgerService, runtime: CoordinatorRuntime, *, run_id: str
) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    prefix = f"{run_id}:"
    for projection in ledger.list_projections(aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE):
        if not projection.aggregate_id.startswith(prefix):
            continue
        state = projection.state
        node_id = projection.aggregate_id.removeprefix(prefix)
        nodes.append(
            {
                "node_id": node_id,
                "attempt_id": state.get("attempt_id"),
                "operation_id": state.get("operation_id"),
                "status": state.get("status"),
                "artifacts": state.get("artifact_references", []),
                "wakeup": state.get("wakeup"),
                "worker": (
                    worker_report(ledger, runtime, run_id=run_id, node_id=node_id)
                    if state.get("status") == "running"
                    else None
                ),
            }
        )
    return sorted(nodes, key=lambda node: str(node["node_id"]))


def worker_report(
    ledger: LedgerService, runtime: CoordinatorRuntime, *, run_id: str, node_id: str
) -> dict[str, object] | None:
    """Report what the persisted worker identity proves, read-only.

    ``liveness`` separates a dead worker (``exited``/``pid_reused``) from a live worker whose
    coordinator lapsed (``alive`` with ``lease_epoch_active`` false: the epoch that launched
    the worker, not whichever coordinator owns the ledger now); a record from
    another host or boot is ``foreign_host`` and is never probed locally.
    """
    record = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key=f"{run_id}:{node_id}"
    )
    if record is None:
        return None
    assessment = assess_worker(record.state)
    identity = assessment.identity
    lease = ledger.read_lease(run_id=run_id, node_id=node_id)
    epoch = runtime.coordinator.current_epoch()
    return {
        "liveness": assessment.liveness.value,
        "supervisor_status": record.state.get("status"),
        "attempt_id": record.state.get("attempt_id"),
        "pid": None if identity is None else identity.pid,
        "process_group_id": None if identity is None else identity.process_group_id,
        "start_identity": None if identity is None else identity.start_identity,
        "host": (
            None
            if identity is None or identity.host is None
            else {
                "machine_id": identity.host.machine_id,
                "boot_id": identity.host.boot_id,
                "hostname": identity.host.hostname,
            }
        ),
        "identity_version": record.state.get("identity_version", 1),
        "lease_epoch": None if lease is None else lease.epoch,
        "lease_expires_at": None if lease is None else lease.expires_at,
        "coordinator_epoch": None if epoch is None else epoch.epoch,
        "lease_epoch_active": runtime.lease_epoch_active(
            run_id=run_id, node_id=node_id, now=_now()
        ),
    }


def _now() -> datetime:
    return datetime.now(tz=UTC)


def start_bound_run(
    ledger: LedgerService,
    *,
    owner: str,
    request: Stage1RunRequest,
    raw_request: dict[str, object],
) -> Stage1Run:
    """Start a durable run, requiring its bound configuration before a new projection."""
    if (
        ledger.read_projection(aggregate_type=RUN_AGGREGATE_TYPE, aggregate_id=str(request.run.id))
        is None
    ):
        require_bound_configuration(raw_request, request)
        if request.behavioral_checks:
            bind_behavioral_checks(request)
    runtime = CoordinatorRuntime(ledger, owner=owner)
    return read_only_service(runtime=runtime, ledger=ledger, request=request).start(
        request, now=_now(), heartbeat_window=HEARTBEAT_WINDOW
    )


def watch_document(
    ledger: LedgerService,
    runtime: CoordinatorRuntime,
    progression: Stage1Progression,
    *,
    action_taken: str | None = None,
) -> dict[str, object]:
    """Render the shared read-only lifecycle view without advancing the run."""
    run = progression.run
    return {
        "run_id": str(run.request.run.id),
        "status": run.status.value,
        "aggregate_version": run.aggregate_version,
        "action_taken": action_taken,
        "next_action": progression.action.value,
        "nodes": nodes(ledger, runtime, run_id=str(run.request.run.id)),
        "hold": None if progression.hold is None else progression.hold.to_state(),
        "execution": execution_report(
            ledger,
            run,
            hold=progression.hold,
            observed_at_resume=progression.observed_host_version,
        ),
        "cost_budget": cost_budget_report(run.request),
    }
