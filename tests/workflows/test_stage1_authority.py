"""Stage 1 authority separation: launch environment, publication, validation and budgets.

A new run's worker holds no ambient host environment and no source-host authority: it commits
locally, and the operator side verifies and publishes its branch. These tests drive the real
``Stage1RunService`` over a real ledger, real git repositories and a real local remote, and
assert on what a launched process actually received and on what the remote actually holds.
A run persisted before trust modes keeps its original contract, and is asserted to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from tests.support.branch_publication import RecordingBranchPublisher
from tests.support.verified_implementation import git
from tests.workflows.test_stage1_isolated import (
    HEARTBEAT,
    NOW,
    RUN_ID,
    Harnessed,
    RecordingIsolatedRunner,
    ScriptedAdmission,
    _build,
    _complete_through_implement,
    _dispatch,
    _host_marker_command,
    _isolated,
    _qualify,
    _reap,
    _verify_implementation,
)
from tests.workflows.test_stage1_runtime import RecordingPullRequests

from enginery.adapters.git_publication import GitBranchPublisher
from enginery.application import branch_publication
from enginery.application.branch_publication import (
    BranchPublicationPort,
    BranchPublicationRequest,
    PublishedBranch,
)
from enginery.application.work_ports import PullRequestPort, PullRequestRequest, PullRequestSnapshot
from enginery.application.worker_environment import LAUNCHER_KEYS, TRUSTED_LOCAL_WORKER_KEYS
from enginery.cli.stage1 import _branch_publisher
from enginery.domain.errors import (
    ExternalConflictError,
    HumanActionRequiredError,
    InvalidInputError,
    MissingPrerequisiteError,
    WorkerFailureError,
)
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.runner import LEGACY_RUNNER_SELECTION, RunnerSelection
from enginery.engine.runtime import (
    BRANCH_PUBLICATION_OPERATOR,
    BRANCH_PUBLICATION_WORKER,
    FixtureDispatch,
    WorkflowNodeDispatch,
)
from enginery.engine.task_repository import intake_path_for, validation_path_for
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.review import ReviewReport
from enginery.workflows.stage1 import (
    DEFAULT_VALIDATION_COMMAND,
    Stage1ProgressionAction,
    Stage1RunService,
    stage1_request_from_state,
)
from enginery.workflows.stage1_runtime import Stage1ValidationResult

BRANCH = "enginery/run-isolated"
_AMBIENT_SECRETS = {
    "GH_TOKEN": "ambient-gh-secret",
    "GITHUB_TOKEN": "ambient-github-secret",
    "SSH_AUTH_SOCK": "/tmp/ambient-agent.sock",
    "AWS_SECRET_ACCESS_KEY": "ambient-aws-secret",
    "UV_PUBLISH_TOKEN": "ambient-publish-secret",
    "NPM_TOKEN": "ambient-npm-secret",
    "ANTHROPIC_API_KEY": "ambient-model-secret",
    "ENGINERY_GATEWAY_TOKEN_FILE": "/tmp/ambient-gateway-token",
}
_PLATFORM_ADDITIONS = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
_DUMP_ENVIRONMENT = (
    "import json, os, sys; open(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))"
)


@pytest.fixture(autouse=True)
def _quiet_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give git a home without a user configuration, and the host an ambient environment."""
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LANG", "C.UTF-8")
    for name, value in _AMBIENT_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENGINERY_AMBIENT_MARKER", "ambient-marker")


# --- helpers ---------------------------------------------------------------------------------


def _register_legacy(built: Harnessed, *, cost_budget: Decimal | None = None) -> Harnessed:
    """Persist the run the way a run started before trust modes was, and return it."""
    request = replace(
        built.request,
        runner=LEGACY_RUNNER_SELECTION,
        implementation=replace(built.request.implementation, cost_budget=cost_budget),
    )
    built.runtime.register_run(
        run_id=str(request.run.id),
        initial_state=request.initial_state(),
        now=NOW,
        heartbeat_window=HEARTBEAT,
    )
    return replace(built, request=request)


def _dump_worker(built: Harnessed, destination: Path) -> None:
    built.harness.command = (sys.executable, "-c", _DUMP_ENVIRONMENT, str(destination))


def _worker_environment(destination: Path) -> dict[str, str]:
    loaded = json.loads(destination.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return {str(key): str(value) for key, value in loaded.items()}


def _node(ledger: LedgerService, node_id: str) -> dict[str, object] | None:
    projection = ledger.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:{node_id}"
    )
    return None if projection is None else dict(projection.state)


def _record_verified_head(
    ledger: LedgerService, attempt_id: str, head_revision: str, *, correlation: str
) -> None:
    ledger.append(
        AppendCommand(
            correlation_id=correlation,
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=attempt_id,
                    expected_version=0,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={"result": {"head_revision": head_revision}},
                ),
            ),
        )
    )


# --- the launch environment -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("isolated", "expected_keys"),
    [(False, TRUSTED_LOCAL_WORKER_KEYS), (True, LAUNCHER_KEYS)],
    ids=["trusted-local", "isolated-oci"],
)
def test_a_new_runs_worker_is_launched_with_only_its_allowlist_and_none_of_the_ambient_secrets(
    ledger_service: LedgerService,
    tmp_path: Path,
    isolated: bool,
    expected_keys: tuple[str, ...],
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated() if isolated else RunnerSelection.trusted_local(),
        admission=ScriptedAdmission() if isolated else None,
    )
    dump = tmp_path / "worker-environment.json"
    _dump_worker(built, dump)
    _qualify(built)

    dispatch = _dispatch(built)
    _reap(dispatch)

    environment = _worker_environment(dump)
    assert set(environment) <= set(expected_keys) | _PLATFORM_ADDITIONS, sorted(environment)
    for name in (*_AMBIENT_SECRETS, "ENGINERY_AMBIENT_MARKER"):
        assert name not in environment
    assert environment["PATH"] == os.environ["PATH"]
    assert environment["HOME"] == os.environ["HOME"]
    request = built.runtime.read_node_request(run_id=RUN_ID, node_id="implement")
    assert request.environment_keys == expected_keys


def test_a_run_persisted_before_trust_modes_still_launches_with_the_ambient_environment(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _register_legacy(
        _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())
    )
    dump = tmp_path / "worker-environment.json"
    _dump_worker(built, dump)
    _qualify(built)

    dispatch = _dispatch(built)
    _reap(dispatch)

    environment = _worker_environment(dump)
    # The documented legacy path: it inherits so that a persisted in-flight run resumes unchanged.
    assert environment["ENGINERY_AMBIENT_MARKER"] == "ambient-marker"
    assert environment["GH_TOKEN"] == _AMBIENT_SECRETS["GH_TOKEN"]
    request = built.runtime.read_node_request(run_id=RUN_ID, node_id="implement")
    assert request.environment_keys is None
    assert request.branch_publication == BRANCH_PUBLICATION_WORKER
    node = _node(ledger_service, "implement")
    assert node is not None
    assert "environment_keys" not in node
    assert "branch_publication" not in node


@pytest.mark.parametrize("isolated", [False, True], ids=["trusted-local", "isolated-oci"])
def test_only_the_implement_node_of_a_new_run_carries_the_environment_and_publication_state(
    ledger_service: LedgerService, tmp_path: Path, isolated: bool
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated() if isolated else RunnerSelection.trusted_local(),
        admission=ScriptedAdmission() if isolated else None,
    )
    _qualify(built)
    dispatch = _dispatch(built)
    _reap(dispatch)

    implement = _node(ledger_service, "implement")
    qualify = _node(ledger_service, "qualify")

    assert implement is not None and qualify is not None
    assert implement["environment_keys"] == list(
        LAUNCHER_KEYS if isolated else TRUSTED_LOCAL_WORKER_KEYS
    )
    assert implement["branch_publication"] == BRANCH_PUBLICATION_OPERATOR
    assert "environment_keys" not in qualify
    assert "branch_publication" not in qualify


# --- the task a worker is given -----------------------------------------------------------


def test_a_new_runs_worker_is_told_to_commit_locally_in_both_modes_and_a_legacy_one_still_pushes(
    tmp_path: Path,
) -> None:
    constraints: list[str] = []
    for index, (runner, admission) in enumerate(
        ((RunnerSelection.trusted_local(), None), (_isolated(), ScriptedAdmission()))
    ):
        directory = tmp_path / f"mode-{index}"
        directory.mkdir()
        with LedgerService.open(directory / "ledger.db") as ledger:
            built = _build(ledger, directory, runner=runner, admission=admission)
            _qualify(built)
            dispatch = _dispatch(built)
            _reap(dispatch)
            constraints.append(dispatch.task.constraints[-1])

    # Both modes are told to commit locally and not push, but only the isolated worker is told
    # it has no remote access and no credentials: a trusted-local worker still runs as the
    # operator, with ambient access, and is never told otherwise.
    for constraint in constraints:
        assert "commit the completed change locally" in constraint
        assert "do not push" in constraint
        assert "the operator publishes the verified branch" in constraint
        assert "push the completed change to origin" not in constraint
    assert "no remote access and no credentials" not in constraints[0]
    assert "credential" not in constraints[0] and "remote access" not in constraints[0]
    assert "no remote access and no credentials" in constraints[1]

    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    with LedgerService.open(legacy_directory / "ledger.db") as ledger:
        legacy = _register_legacy(
            _build(ledger, legacy_directory, runner=RunnerSelection.trusted_local())
        )
        _qualify(legacy)
        legacy_dispatch = _dispatch(legacy)
        _reap(legacy_dispatch)
        assert legacy_dispatch.task.constraints[-1].endswith(
            "commit and push the completed change to origin."
        )


# --- hard-dollar budgets ------------------------------------------------------------------


@pytest.mark.parametrize("isolated", [False, True], ids=["trusted-local", "isolated-oci"])
def test_a_new_run_that_binds_a_hard_dollar_budget_is_refused_and_leaves_nothing(
    ledger_service: LedgerService, tmp_path: Path, isolated: bool
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated() if isolated else RunnerSelection.trusted_local(),
        admission=ScriptedAdmission() if isolated else None,
    )
    request = replace(
        built.request,
        implementation=replace(built.request.implementation, cost_budget=Decimal("5.0")),
    )

    with pytest.raises(
        InvalidInputError, match="no supported harness enforces a hard-dollar budget"
    ):
        built.service.start(request, now=NOW, heartbeat_window=HEARTBEAT)

    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None
    assert ledger_service.list_projections(aggregate_type="runtime_node") == ()


def test_dispatch_refuses_a_hard_dollar_budget_that_slipped_into_a_persisted_new_run(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())
    smuggled = replace(
        built.request,
        implementation=replace(built.request.implementation, cost_budget=Decimal("5.0")),
    )
    # Bypassing start leaves a persisted new run whose request binds a number nothing enforces.
    built.runtime.register_run(
        run_id=str(smuggled.run.id),
        initial_state=smuggled.initial_state(),
        now=NOW,
        heartbeat_window=HEARTBEAT,
    )
    built = replace(built, request=smuggled)
    _qualify(built)

    with pytest.raises(
        InvalidInputError, match="no supported harness enforces a hard-dollar budget"
    ):
        _dispatch(built)

    assert _node(ledger_service, "implement") is None
    assert ledger_service.read_lease(run_id=RUN_ID, node_id="implement") is None
    assert not built.request.workspace_path.exists()


def test_a_persisted_legacy_run_keeps_its_carried_budget_decodes_and_still_dispatches(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _register_legacy(
        _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local()),
        cost_budget=Decimal("5.0"),
    )
    state = built.request.initial_state()
    implementation = state["implementation"]
    assert isinstance(implementation, dict) and implementation["cost_budget"] == "5.0"
    assert "runner" not in state

    decoded = stage1_request_from_state(json.loads(json.dumps(state)))
    assert decoded == built.request
    assert decoded.digest == built.request.digest
    assert decoded.implementation.cost_budget == Decimal("5.0")
    assert built.service.read(built.request.run.id).request.digest == built.request.digest

    _qualify(built)
    dispatch = _dispatch(built)
    _reap(dispatch)

    assert dispatch.task.cost_budget == Decimal("5.0")
    assert dispatch.fixture is not None


def test_a_new_run_dispatches_with_no_cost_budget(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())
    _qualify(built)

    dispatch = _dispatch(built)
    _reap(dispatch)

    assert dispatch.task.cost_budget is None


# --- validation ---------------------------------------------------------------------------


@dataclass
class InspectingIsolatedRunner:
    """An isolated runner that records what the checkout it was handed contains when it runs."""

    seen: list[dict[str, object]] = field(default_factory=list)
    returncode: int = 0
    failure: Exception | None = None

    def __call__(
        self, command: tuple[str, ...], workspace: Path
    ) -> subprocess.CompletedProcess[str]:
        self.seen.append(
            {
                "workspace": workspace,
                "files": sorted(p.name for p in workspace.iterdir() if p.name != ".git"),
                "feature": (workspace / "feature.txt").read_text(encoding="utf-8")
                if (workspace / "feature.txt").exists()
                else None,
                "head": git("rev-parse", "HEAD", cwd=workspace),
                "status": git("status", "--porcelain", cwd=workspace),
                "remotes": git("remote", cwd=workspace),
            }
        )
        if self.failure is not None:
            raise self.failure
        return subprocess.CompletedProcess(command, self.returncode, "output\n", "")


def _isolated_ready_to_validate(
    ledger: LedgerService, tmp_path: Path, runner: object
) -> tuple[Harnessed, str]:
    built = _build(
        ledger,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=cast(RecordingIsolatedRunner, runner),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    return built, _verify_implementation(built)


def _validate(built: Harnessed) -> Stage1ValidationResult:
    return built.service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )


def test_isolated_validation_sees_the_verified_head_and_not_what_the_worker_left_behind(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    inspector = InspectingIsolatedRunner()
    built, head = _isolated_ready_to_validate(ledger_service, tmp_path, inspector)
    workspace = built.request.workspace_path
    # The live workspace holds what a hostile or careless worker can leave: an uncommitted edit
    # of a committed file and scratch files. None of it was verified.
    (workspace / "feature.txt").write_text("tampered after the commit\n", encoding="utf-8")
    assert (workspace / "scratch.txt").exists()

    _validate(built)

    (seen,) = inspector.seen
    checkout = validation_path_for(workspace)
    assert seen["workspace"] == checkout
    assert checkout != workspace
    assert seen["head"] == head
    assert seen["feature"] == "implemented\n", "the committed content, not the tampered edit"
    assert "scratch.txt" not in cast(list[str], seen["files"])
    assert seen["status"] == "", "a clean checkout of exactly the verified revision"
    assert seen["remotes"] == "", "the checkout carries no remote"
    assert not checkout.exists(), "the checkout is removed afterwards"
    node = _node(ledger_service, "validate")
    assert node is not None
    assert node["status"] == "passed"
    assert node["validated_revision"] == head


def test_the_verified_head_checkout_is_removed_after_a_failing_validation(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    inspector = InspectingIsolatedRunner(returncode=1)
    built, _ = _isolated_ready_to_validate(ledger_service, tmp_path, inspector)

    result = _validate(built)

    assert result.passed is False
    assert not validation_path_for(built.request.workspace_path).exists()
    node = _node(ledger_service, "validate")
    assert node is not None and node["status"] == "failed"


def test_the_verified_head_checkout_is_removed_when_the_isolated_runner_itself_fails(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    inspector = InspectingIsolatedRunner(failure=WorkerFailureError("runner refused to launch"))
    built, _ = _isolated_ready_to_validate(ledger_service, tmp_path, inspector)

    with pytest.raises(WorkerFailureError, match="refused to launch"):
        _validate(built)

    assert not validation_path_for(built.request.workspace_path).exists()
    node = _node(ledger_service, "validate")
    assert node is not None
    assert node["status"] == "queued", "an unrun validation is never recorded as passed"


def test_a_stale_checkout_left_by_a_crashed_validation_is_replaced_not_reused(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    inspector = InspectingIsolatedRunner()
    built, head = _isolated_ready_to_validate(ledger_service, tmp_path, inspector)
    stale = validation_path_for(built.request.workspace_path)
    stale.mkdir()
    (stale / "leftover-from-a-crash.txt").write_text("stale\n", encoding="utf-8")

    _validate(built)

    (seen,) = inspector.seen
    assert "leftover-from-a-crash.txt" not in cast(list[str], seen["files"])
    assert seen["head"] == head
    assert not stale.exists()


def test_isolated_validation_without_a_verified_head_runs_nothing_and_records_nothing(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runner = RecordingIsolatedRunner()
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=runner,
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)  # implement passed, but no verified head was recorded

    with pytest.raises(MissingPrerequisiteError, match="verified head revision"):
        _validate(built)

    assert runner.calls == []
    assert _node(ledger_service, "validate") is None
    assert not validation_path_for(built.request.workspace_path).exists()


def _env_dump_validation(destination: Path) -> tuple[tuple[str, ...], ...]:
    return ((sys.executable, "-c", _DUMP_ENVIRONMENT, str(destination)),)


def test_trusted_local_validation_of_a_new_run_runs_with_the_allowlist_not_the_ambient_environment(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    dump = tmp_path / "validation-environment.json"
    built = _build(
        ledger_service,
        tmp_path,
        runner=RunnerSelection.trusted_local(),
        validation_commands=_env_dump_validation(dump),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    built.request.workspace_path.mkdir(parents=True)
    _complete_through_implement(built)

    result = _validate(built)

    assert result.passed is True
    environment = _worker_environment(dump)
    assert set(environment) <= set(TRUSTED_LOCAL_WORKER_KEYS) | _PLATFORM_ADDITIONS
    for name in (*_AMBIENT_SECRETS, "ENGINERY_AMBIENT_MARKER"):
        assert name not in environment


def test_validation_of_a_run_persisted_before_trust_modes_keeps_its_ambient_environment(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    dump = tmp_path / "validation-environment.json"
    built = _register_legacy(
        _build(
            ledger_service,
            tmp_path,
            runner=RunnerSelection.trusted_local(),
            validation_commands=_env_dump_validation(dump),
            deterministic_implement=True,
        )
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    built.request.workspace_path.mkdir(parents=True)
    _complete_through_implement(built)

    _validate(built)

    environment = _worker_environment(dump)
    assert environment["ENGINERY_AMBIENT_MARKER"] == "ambient-marker"
    assert environment["GH_TOKEN"] == _AMBIENT_SECRETS["GH_TOKEN"]


# --- publication --------------------------------------------------------------------------


class EventfulPullRequests(RecordingPullRequests):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def create_or_update(self, request: PullRequestRequest) -> PullRequestSnapshot:
        self.events.append("pull_request")
        return super().create_or_update(request)


class EventfulPublisher:
    """Wraps the real publisher, recording the order effects happen in and what was asked."""

    def __init__(self, inner: GitBranchPublisher, events: list[str]) -> None:
        self.inner = inner
        self.events = events
        self.requests: list[BranchPublicationRequest] = []

    def probe(self) -> object:
        return self.inner.probe()

    def changed_paths(self, *, base_revision: str, head_revision: str) -> tuple[str, ...]:
        return self.inner.changed_paths(base_revision=base_revision, head_revision=head_revision)

    def publish(self, request: BranchPublicationRequest) -> PublishedBranch:
        self.events.append("publish")
        self.requests.append(request)
        return self.inner.publish(request)

    def reconcile(self, request: BranchPublicationRequest) -> ReconciliationResult:
        return self.inner.reconcile(request)


@dataclass
class Ready:
    built: Harnessed
    service: Stage1RunService
    remote: Path
    head: str
    pulls: EventfulPullRequests
    publisher: EventfulPublisher
    events: list[str]
    pushes: Path

    def open(self) -> None:
        self.service.open_pull_request(
            self.built.request, now=NOW + timedelta(seconds=9), heartbeat_window=HEARTBEAT
        )

    def remote_branch(self) -> str | None:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}"],
            cwd=self.remote,
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.stdout.strip() or None

    def accepted_pushes(self) -> list[str]:
        return self.pushes.read_text(encoding="utf-8").splitlines() if self.pushes.exists() else []


def _give_origin(repository: Path) -> tuple[Path, Path]:
    remote = repository.parent / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=repository.parent)
    pushes = repository.parent / "accepted-pushes.log"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        f'#!/bin/sh\nwhile read old new ref; do echo "$ref" >> {pushes}; done\n', encoding="utf-8"
    )
    hook.chmod(0o755)
    git("remote", "add", "origin", str(remote), cwd=repository)
    git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repository)
    pushes.unlink(missing_ok=True)
    return remote, pushes


def _isolated_ready_to_open(
    ledger: LedgerService,
    tmp_path: Path,
    *,
    attempt_id: str = "implement-0",
    committed: Mapping[str, str] | None = None,
    protected_paths: tuple[str, ...] = (),
) -> Ready:
    runner = RecordingIsolatedRunner()
    built = _build(
        ledger,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=runner,
        deterministic_implement=True,
        attempt_id=attempt_id,
    )
    if protected_paths:
        built.request = replace(built.request, protected_paths=protected_paths)
    remote, pushes = _give_origin(built.repository)
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    head = _verify_implementation(built, committed=committed)
    built.service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )
    built.service.review_implementation(
        built.request,
        ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
        repair_attempt=0,
        now=NOW + timedelta(seconds=4),
        heartbeat_window=HEARTBEAT,
    )
    events: list[str] = []
    pulls = EventfulPullRequests(events)
    real = _branch_publisher(built.request)
    assert real is not None
    publisher = EventfulPublisher(real, events)
    service = replace(
        built.service,
        pull_requests=cast(PullRequestPort, pulls),
        branch_publisher=cast(BranchPublicationPort, publisher),
    )
    return Ready(built, service, remote, head, pulls, publisher, events, pushes)


def _rival_push(ready: Ready, name: str = "rival.txt") -> str:
    clone = ready.remote.parent / f"rival-{name}"
    git("clone", "-q", str(ready.remote), str(clone), cwd=ready.remote.parent)
    git("config", "user.email", "rival@example.test", cwd=clone)
    git("config", "user.name", "Rival", cwd=clone)
    (clone / name).write_text("someone else\n", encoding="utf-8")
    git("add", name, cwd=clone)
    git("commit", "-q", "-m", "rival", cwd=clone)
    git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone)


def test_the_verified_head_is_published_from_the_intake_before_the_pull_request_is_created(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    workspace = ready.built.request.workspace_path
    assert git("remote", cwd=workspace) == "", "the worker's own repository has no remote"

    ready.open()

    assert ready.events == ["publish", "pull_request"]
    assert ready.remote_branch() == ready.head
    assert ready.accepted_pushes() == [f"refs/heads/{BRANCH}"]
    (request,) = ready.publisher.requests
    assert request.head_revision == ready.head
    assert request.branch == BRANCH
    assert request.expected_remote_revision is None
    assert request.repository_id == "repository-1"
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    assert open_pr["status"] == "passed"
    assert open_pr["published_revision"] == ready.head
    assert open_pr["pull_request_number"] == 17
    (pull_request,) = ready.pulls.requests
    assert pull_request.head_branch == BRANCH


def test_a_new_run_without_a_publisher_stops_before_registering_the_pull_request_node(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    without = replace(ready.service, branch_publisher=None)

    with pytest.raises(MissingPrerequisiteError, match="branch publisher is not configured"):
        without.open_pull_request(
            ready.built.request, now=NOW + timedelta(seconds=9), heartbeat_window=HEARTBEAT
        )

    assert ready.events == []
    assert _node(ledger_service, "open_pr") is None
    assert ready.remote_branch() is None


def test_a_remote_branch_someone_else_moved_is_never_overwritten_and_no_pull_request_is_made(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    rival = _rival_push(ready)
    pushes_before = ready.accepted_pushes()

    with pytest.raises(ExternalConflictError, match="not overwritten"):
        ready.open()

    assert ready.events == ["publish"], "no pull request is created for an unpublished branch"
    assert ready.remote_branch() == rival
    assert ready.accepted_pushes() == pushes_before
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None and open_pr["status"] == "queued"
    assert (
        ready.built.service.next_action(ready.built.request.run.id).action
        is Stage1ProgressionAction.OPEN_PR
    ), "the intent is durable and the step is retried, never skipped"


def test_a_pull_request_failure_after_publication_is_retried_without_pushing_again(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    ready.pulls.failures_remaining = 1

    with pytest.raises(ExternalConflictError, match="simulated provider interruption"):
        ready.open()
    assert ready.remote_branch() == ready.head
    ready.open()

    assert ready.events == ["publish", "pull_request", "publish", "pull_request"]
    assert ready.accepted_pushes() == [f"refs/heads/{BRANCH}"], "the remote accepted one push"
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None and open_pr["status"] == "passed"


def test_a_publisher_failure_is_retried_to_success_once_the_conflict_is_gone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    _rival_push(ready)
    with pytest.raises(ExternalConflictError):
        ready.open()
    git("update-ref", "-d", f"refs/heads/{BRANCH}", cwd=ready.remote)

    ready.open()

    assert ready.remote_branch() == ready.head
    assert ready.events == ["publish", "publish", "pull_request"]


def _advance_to_repair_generation(ready: Ready, *, generation: int = 1) -> str:
    """Leave a further, verified implementation attempt: what a repair generation produces."""
    built = ready.built
    repository = built.repository
    git("switch", "-q", "-c", "repair-source", built.request.run.base_revision, cwd=repository)
    (repository / "repair.txt").write_text("repaired\n", encoding="utf-8")
    git("add", "repair.txt", cwd=repository)
    git("commit", "-q", "-m", "repair", cwd=repository)
    repaired = git("rev-parse", "HEAD", cwd=repository)
    attempt_id = f"{RUN_ID}-implement-{generation}"
    node = built.runtime.read_node_request(run_id=RUN_ID, node_id="implement")
    dispatch = WorkflowNodeDispatch(
        FixtureDispatch(
            run_id=RUN_ID,
            node_id="implement",
            attempt_id=attempt_id,
            repository_id=node.repository_id,
            repository_path=node.repository_path,
            workspace_path=node.workspace_path,
            base_revision=node.base_revision,
            command=("implement",),
            expected_attempt_version=0,
            operation_id=f"implement-repair:{RUN_ID}:{generation}",
            dependencies=node.dependencies,
            workflow_definition_id=node.workflow_definition_id,
            workspace_mode=node.workspace_mode,
        ),
        built.request.manifest,
    )
    epoch = built.runtime.register_node(
        dispatch=dispatch, now=NOW + timedelta(seconds=10), heartbeat_window=HEARTBEAT
    )
    built.runtime.complete_node(
        run_id=RUN_ID, node_id="implement", epoch=epoch.epoch, now=NOW + timedelta(seconds=10)
    )
    _record_verified_head(
        built.ledger,
        attempt_id,
        repaired,
        correlation=f"implementation-verified-head-{generation}",
    )
    return repaired


def test_a_repair_generation_publishes_only_over_the_head_its_previous_generation_published(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    ready.open()
    repaired = _advance_to_repair_generation(ready)

    ready.open()

    first, second = ready.publisher.requests
    assert first.expected_remote_revision is None
    assert second.head_revision == repaired
    assert second.expected_remote_revision == ready.head, "the previous generation's verified head"
    assert second.operation_id != first.operation_id, "one publication operation per generation"
    assert ready.remote_branch() == repaired
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    assert open_pr["published_revision"] == repaired
    assert open_pr["attempt_id"] == "open-pr-1"


def test_a_repair_generation_never_overwrites_a_remote_branch_that_moved_in_between(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    ready.open()
    _advance_to_repair_generation(ready)
    rival = _rival_push(ready)

    with pytest.raises(ExternalConflictError, match="not overwritten"):
        ready.open()

    assert ready.remote_branch() == rival
    assert len(ready.pulls.requests) == 1, "no second pull request for an unpublished repair"


def test_a_repair_publication_survives_the_node_being_re_registered_before_it_retries(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    """The expectation comes from the ledger-recorded verified attempts, not the node state.

    Re-registering the open-pull-request node for the repair generation replaces its state, which
    loses anything recorded on it. A crash between that and the push must not strand the run.
    """
    ready = _isolated_ready_to_open(ledger_service, tmp_path)
    ready.open()
    repaired = _advance_to_repair_generation(ready)
    flaky = _FailingOnce(ready.publisher.inner)
    ready.service = replace(ready.service, branch_publisher=cast(BranchPublicationPort, flaky))

    with pytest.raises(ExternalConflictError, match="simulated crash"):
        ready.open()
    node = _node(ledger_service, "open_pr")
    assert node is not None and "published_revision" not in node, "the node state was replaced"
    ready.open()

    assert ready.remote_branch() == repaired


class _FailingOnce:
    def __init__(self, inner: GitBranchPublisher) -> None:
        self.inner = inner
        self.failed = False

    def probe(self) -> object:
        return self.inner.probe()

    def changed_paths(self, *, base_revision: str, head_revision: str) -> tuple[str, ...]:
        return self.inner.changed_paths(base_revision=base_revision, head_revision=head_revision)

    def publish(self, request: BranchPublicationRequest) -> PublishedBranch:
        if not self.failed:
            self.failed = True
            raise ExternalConflictError("simulated crash before the push")
        return self.inner.publish(request)

    def reconcile(self, request: BranchPublicationRequest) -> ReconciliationResult:
        return self.inner.reconcile(request)


def test_a_publisher_that_reports_another_revision_is_not_trusted(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path)

    class Liar:
        def probe(self) -> object:
            raise AssertionError("unused")

        def changed_paths(self, *, base_revision: str, head_revision: str) -> tuple[str, ...]:
            return ()

        def publish(self, request: BranchPublicationRequest) -> PublishedBranch:
            return PublishedBranch(branch=request.branch, revision="9" * 40)

        def reconcile(self, request: BranchPublicationRequest) -> ReconciliationResult:
            return ReconciliationResult.NOT_FOUND

    lying = replace(ready.service, branch_publisher=cast(BranchPublicationPort, Liar()))

    with pytest.raises(ExternalConflictError, match="did not report the verified branch"):
        lying.open_pull_request(
            ready.built.request, now=NOW + timedelta(seconds=9), heartbeat_window=HEARTBEAT
        )

    assert ready.events == []
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None and open_pr["status"] == "queued"


def test_a_run_persisted_before_operator_publication_publishes_nothing(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    marker = tmp_path / "legacy-validation"
    built = _register_legacy(
        _build(
            ledger_service,
            tmp_path,
            runner=RunnerSelection.trusted_local(),
            validation_commands=(_host_marker_command(marker),),
            deterministic_implement=True,
        )
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    built.request.workspace_path.mkdir(parents=True)
    _complete_through_implement(built)
    events: list[str] = []
    publisher = RecordingBranchPublisher()
    service = replace(
        built.service,
        pull_requests=cast(PullRequestPort, EventfulPullRequests(events)),
        branch_publisher=publisher,
    )
    service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )
    service.review_implementation(
        built.request,
        ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
        repair_attempt=0,
        now=NOW + timedelta(seconds=4),
        heartbeat_window=HEARTBEAT,
    )

    service.open_pull_request(
        built.request, now=NOW + timedelta(seconds=9), heartbeat_window=HEARTBEAT
    )

    assert events == ["pull_request"]
    assert publisher.requests == []
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    assert open_pr["status"] == "passed"
    assert "published_revision" not in open_pr


def test_a_run_persisted_before_trust_modes_is_given_no_publisher(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())

    assert _branch_publisher(replace(built.request, runner=LEGACY_RUNNER_SELECTION)) is None


def test_the_cli_wires_the_publisher_to_the_repository_the_worker_cannot_write(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    isolated = _build(
        ledger_service, tmp_path / "isolated", runner=_isolated(), admission=ScriptedAdmission()
    ).request
    trusted = _build(
        ledger_service, tmp_path / "trusted", runner=RunnerSelection.trusted_local()
    ).request

    isolated_publisher = _branch_publisher(isolated)
    trusted_publisher = _branch_publisher(trusted)

    assert isinstance(isolated_publisher, GitBranchPublisher)
    assert isolated_publisher.source_path == intake_path_for(isolated.workspace_path)
    assert isolated_publisher.source_path != isolated.workspace_path
    assert isolated_publisher.repository_path == isolated.repository_path
    assert isinstance(trusted_publisher, GitBranchPublisher)
    assert trusted_publisher.source_path == trusted.workspace_path
    assert trusted_publisher.repository_path == trusted.repository_path


def test_a_trusted_local_run_publishes_the_verified_head_from_its_workspace(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    marker = tmp_path / "validation-ran"
    built = _build(
        ledger_service,
        tmp_path,
        runner=RunnerSelection.trusted_local(),
        validation_commands=(_host_marker_command(marker),),
        deterministic_implement=True,
    )
    remote, pushes = _give_origin(built.repository)
    workspace = built.request.workspace_path
    git(
        "worktree",
        "add",
        "--detach",
        str(workspace),
        built.request.run.base_revision,
        cwd=built.repository,
    )
    git("switch", "-q", "-c", BRANCH, cwd=workspace)
    (workspace / "feature.txt").write_text("implemented\n", encoding="utf-8")
    git("add", "feature.txt", cwd=workspace)
    git("commit", "-q", "-m", "implement", cwd=workspace)
    head = git("rev-parse", "HEAD", cwd=workspace)
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    _record_verified_head(ledger_service, "implement-0", head, correlation="verified-head-trusted")
    events: list[str] = []
    real = _branch_publisher(built.request)
    assert real is not None
    service = replace(
        built.service,
        pull_requests=cast(PullRequestPort, EventfulPullRequests(events)),
        branch_publisher=cast(BranchPublicationPort, EventfulPublisher(real, events)),
    )
    service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )
    service.review_implementation(
        built.request,
        ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
        repair_attempt=0,
        now=NOW + timedelta(seconds=4),
        heartbeat_window=HEARTBEAT,
    )

    service.open_pull_request(
        built.request, now=NOW + timedelta(seconds=9), heartbeat_window=HEARTBEAT
    )

    assert events == ["publish", "pull_request"]
    assert git("rev-parse", f"refs/heads/{BRANCH}", cwd=remote) == head
    assert pushes.read_text(encoding="utf-8").splitlines() == [f"refs/heads/{BRANCH}"]
    node = _node(ledger_service, "open_pr")
    assert node is not None and node["published_revision"] == head


# --- protected paths: a held publication -----------------------------------------------------

_WORKFLOW = {".github/workflows/x.yml": "name: x\non: push\njobs: {}\n"}


def _decide(ready: Ready, decision: str, *, reason: str = "reviewed the workflow change") -> None:
    """Record the operator's decision exactly as ``stage1 approve``/``stage1 reject`` does."""
    runtime = ready.built.runtime
    now = NOW + timedelta(seconds=11)
    epoch = runtime.claim_epoch(now=now, heartbeat_window=HEARTBEAT)
    approved = decision == "approve"
    runtime.resolve_human_wait(
        run_id=RUN_ID,
        node_id="open_pr",
        epoch=epoch.epoch,
        now=now,
        outcome="passed" if approved else "failed",
        extra={
            "operator_decision": "approved" if approved else "rejected",
            "reason": reason,
        },
    )


def _next_action(ready: Ready) -> Stage1ProgressionAction:
    return ready.built.service.next_action(ready.built.request.run.id).action


def test_a_candidate_editing_a_ci_workflow_is_held_and_nothing_is_pushed(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)

    with pytest.raises(HumanActionRequiredError) as caught:
        ready.open()

    message = str(caught.value)
    assert ".github/workflows/x.yml" in message
    assert "stage1 approve" in message and "stage1 reject" in message
    assert f"--run-id {RUN_ID}" in message and "--node-id open_pr" in message
    assert caught.value.details["approve_command"].startswith("enginery stage1 approve ")
    assert ready.remote_branch() is None, "nothing reached the remote"
    assert ready.accepted_pushes() == []
    assert ready.publisher.requests == [], "the publisher was never asked to push"
    assert ready.events == [] and ready.pulls.requests == []
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None and open_pr["status"] == "awaiting_human"
    record = open_pr["protected_publication"]
    assert isinstance(record, dict)
    assert record["matched_paths"] == [".github/workflows/x.yml"]
    assert record["head_revision"] == ready.head
    assert ".github/**" in record["patterns"] and "CODEOWNERS" in record["patterns"]
    assert record["digest"]
    assert open_pr["human_wait_reason"] == message
    assert _next_action(ready) is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW, (
        "an advance never retries a held publication on its own"
    )

    with pytest.raises(HumanActionRequiredError):
        ready.open()
    assert ready.remote_branch() is None and ready.publisher.requests == []
    assert _node(ledger_service, "open_pr") == open_pr, "a repeated attempt changes nothing"


def test_an_approved_protected_publication_publishes_and_records_the_decision(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)
    with pytest.raises(HumanActionRequiredError):
        ready.open()

    _decide(ready, "approve", reason="the workflow change is intended")

    assert _next_action(ready) is Stage1ProgressionAction.OPEN_PR, (
        "the step is performed, not skipped"
    )
    assert ready.remote_branch() is None, "an approval alone pushes nothing"
    ready.open()

    assert ready.events == ["publish", "pull_request"]
    assert ready.remote_branch() == ready.head
    assert ready.accepted_pushes() == [f"refs/heads/{BRANCH}"]
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    assert open_pr["status"] == "passed"
    assert open_pr["attempt_id"] == "open-pr-approved-0"
    assert open_pr["published_revision"] == ready.head
    assert open_pr["pull_request_number"] == 17
    record = open_pr["protected_publication"]
    assert isinstance(record, dict) and record["matched_paths"] == [".github/workflows/x.yml"]
    approval = open_pr["publication_approval"]
    assert isinstance(approval, dict)
    assert approval["operator_decision"] == "approved"
    assert approval["reason"] == "the workflow change is intended"
    assert approval["approved_protected_publication_digest"] == record["digest"]
    assert _next_action(ready) is Stage1ProgressionAction.WAIT_FOR_CI


def test_a_rejected_protected_publication_is_final_and_pushes_nothing(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)
    with pytest.raises(HumanActionRequiredError):
        ready.open()

    _decide(ready, "reject", reason="not for this issue")

    assert _next_action(ready) is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
    with pytest.raises(ExternalConflictError, match="rejected"):
        ready.open()
    assert ready.remote_branch() is None and ready.publisher.requests == []


def test_a_publication_failure_after_an_approval_resumes_as_approved_not_held_again(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)
    with pytest.raises(HumanActionRequiredError):
        ready.open()
    _decide(ready, "approve")
    held_digest = _held_digest(ledger_service)
    flaky = _FailingOnce(ready.publisher.inner)
    ready.service = replace(ready.service, branch_publisher=cast(BranchPublicationPort, flaky))

    with pytest.raises(ExternalConflictError, match="simulated crash"):
        ready.open()
    interrupted = _node(ledger_service, "open_pr")
    assert interrupted is not None and interrupted["status"] == "queued"
    assert interrupted["attempt_id"] == "open-pr-approved-0"
    assert interrupted["publication_approval"] == {
        "operator_decision": "approved",
        "reason": "reviewed the workflow change",
        "approved_protected_publication_digest": held_digest,
    }, "the approved digest and reason travel with the re-registered node"
    ready.open()

    assert ready.remote_branch() == ready.head
    node = _node(ledger_service, "open_pr")
    assert node is not None and node["status"] == "passed"
    record = node["protected_publication"]
    assert isinstance(record, dict)
    assert node["publication_approval"] == {
        "operator_decision": "approved",
        "reason": "reviewed the workflow change",
        "approved_protected_publication_digest": record["digest"],
    }, "the final record keeps the approval's reason and digest"


def _held_digest(ledger: LedgerService) -> str:
    node = _node(ledger, "open_pr")
    assert node is not None
    record = node["protected_publication"]
    assert isinstance(record, dict)
    return str(record["digest"])


def _widen_protected_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # The request itself is digest-bound, so only a different built-in set (a newer release) can
    # differ from the one the operator approved the matched paths under.
    monkeypatch.setattr(
        branch_publication,
        "DEFAULT_PROTECTED_PATH_PATTERNS",
        (*branch_publication.DEFAULT_PROTECTED_PATH_PATTERNS, "*.yml"),
    )


def test_a_protected_set_that_changed_after_the_approval_is_held_again_on_every_attempt(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)
    with pytest.raises(HumanActionRequiredError):
        ready.open()
    _decide(ready, "approve")
    _widen_protected_set(monkeypatch)

    for attempt in range(2):
        # The first call after the change says why the approval is void; the held node then
        # simply keeps waiting for a decision.
        with pytest.raises(
            HumanActionRequiredError, match="no longer matches" if attempt == 0 else "held"
        ):
            ready.open()

        node = _node(ledger_service, "open_pr")
        assert node is not None and node["status"] == "awaiting_human", (
            "the node waits for a human again, it is never left queued to be advanced"
        )
        assert _next_action(ready) is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
        assert ready.remote_branch() is None and ready.publisher.requests == []
        assert ready.accepted_pushes() == [] and ready.events == []


def test_a_new_approval_of_the_changed_set_publishes_under_its_own_digest_and_reason(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)
    with pytest.raises(HumanActionRequiredError):
        ready.open()
    _decide(ready, "approve", reason="first look")
    first_digest = _held_digest(ledger_service)
    _widen_protected_set(monkeypatch)
    with pytest.raises(HumanActionRequiredError, match="no longer matches"):
        ready.open()
    held = _node(ledger_service, "open_pr")
    assert held is not None and held["protected_publication"]["digest"] != first_digest  # type: ignore[index]

    _decide(ready, "approve", reason="second look at the wider set")
    ready.open()

    node = _node(ledger_service, "open_pr")
    assert node is not None and node["status"] == "passed"
    assert node["attempt_id"] == "open-pr-approved-r2-0"
    assert ready.remote_branch() == ready.head and ready.events == ["publish", "pull_request"]
    record = node["protected_publication"]
    assert isinstance(record, dict) and record["digest"] != first_digest
    assert node["publication_approval"] == {
        "operator_decision": "approved",
        "reason": "second look at the wider set",
        "approved_protected_publication_digest": record["digest"],
    }


def test_a_set_that_changes_between_a_publication_failure_and_its_retry_is_held_again(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, committed=_WORKFLOW)
    with pytest.raises(HumanActionRequiredError):
        ready.open()
    _decide(ready, "approve")
    flaky = _FailingOnce(ready.publisher.inner)
    ready.service = replace(ready.service, branch_publisher=cast(BranchPublicationPort, flaky))
    with pytest.raises(ExternalConflictError, match="simulated crash"):
        ready.open()
    _widen_protected_set(monkeypatch)

    with pytest.raises(HumanActionRequiredError, match="no longer matches"):
        ready.open()

    node = _node(ledger_service, "open_pr")
    assert node is not None and node["status"] == "awaiting_human"
    assert ready.remote_branch() is None and ready.accepted_pushes() == []


def test_an_ordinary_change_publishes_unheld_and_records_the_empty_match(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(
        ledger_service, tmp_path, committed={"src/app.py": "print('x')\n", "docs/a.md": "a\n"}
    )

    ready.open()

    assert ready.events == ["publish", "pull_request"]
    assert ready.remote_branch() == ready.head
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    assert open_pr["attempt_id"] == "open-pr-0"
    assert "publication_approval" not in open_pr
    record = open_pr["protected_publication"]
    assert isinstance(record, dict)
    assert record["matched_paths"] == [] and record["matched_path_count"] == 0
    assert record["changed_path_count"] == 2


@pytest.mark.parametrize(
    "path",
    [
        ".GitHub/Workflows/X.yml",
        "CODEOWNERS",
        "docs/CODEOWNERS",
        ".circleci/config.yml",
        "holdouts/case-1.json",
        ".enginery/policy.json",
        "services/api/Jenkinsfile",
        'ci/odd \u00e9 name".yml',
    ],
)
def test_protected_paths_hold_including_case_variants_and_odd_names(
    ledger_service: LedgerService, tmp_path: Path, path: str
) -> None:
    extra = ("ci/**",) if path.startswith("ci/") else ()
    ready = _isolated_ready_to_open(
        ledger_service, tmp_path, committed={path: "x\n"}, protected_paths=extra
    )

    with pytest.raises(HumanActionRequiredError):
        ready.open()

    assert ready.remote_branch() is None and ready.publisher.requests == []
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    assert open_pr["protected_publication"]["matched_paths"] == [path]  # type: ignore[index]


def test_an_extra_protected_pattern_bound_in_the_request_holds_what_it_names(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(
        ledger_service,
        tmp_path,
        committed={"deploy/prod.yaml": "x\n", "src/app.py": "y\n"},
        protected_paths=("deploy/**",),
    )

    with pytest.raises(HumanActionRequiredError, match=r"deploy/prod\.yaml"):
        ready.open()

    assert ready.remote_branch() is None
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None
    record = open_pr["protected_publication"]
    assert isinstance(record, dict) and record["matched_paths"] == ["deploy/prod.yaml"]
    assert "deploy/**" in record["patterns"]


def test_a_trusted_local_candidate_editing_ci_configuration_is_held_before_any_push(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=RunnerSelection.trusted_local(),
        validation_commands=(_host_marker_command(tmp_path / "validation-ran"),),
        deterministic_implement=True,
    )
    remote, pushes = _give_origin(built.repository)
    workspace = built.request.workspace_path
    git(
        "worktree",
        "add",
        "--detach",
        str(workspace),
        built.request.run.base_revision,
        cwd=built.repository,
    )
    git("switch", "-q", "-c", BRANCH, cwd=workspace)
    (workspace / ".gitlab-ci.yml").write_text("stages: [x]\n", encoding="utf-8")
    git("add", ".gitlab-ci.yml", cwd=workspace)
    git("commit", "-q", "-m", "implement", cwd=workspace)
    head = git("rev-parse", "HEAD", cwd=workspace)
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    _record_verified_head(ledger_service, "implement-0", head, correlation="verified-head-held")
    events: list[str] = []
    real = _branch_publisher(built.request)
    assert real is not None
    service = replace(
        built.service,
        pull_requests=cast(PullRequestPort, EventfulPullRequests(events)),
        branch_publisher=cast(BranchPublicationPort, EventfulPublisher(real, events)),
    )
    service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )
    service.review_implementation(
        built.request,
        ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
        repair_attempt=0,
        now=NOW + timedelta(seconds=4),
        heartbeat_window=HEARTBEAT,
    )

    with pytest.raises(HumanActionRequiredError, match=r"\.gitlab-ci\.yml"):
        service.open_pull_request(
            built.request, now=NOW + timedelta(seconds=9), heartbeat_window=HEARTBEAT
        )

    assert events == []
    assert pushes.exists() is False
    assert BRANCH not in git("for-each-ref", "refs/heads", cwd=remote)


# --- the run's own attempt generation --------------------------------------------------------


@pytest.mark.parametrize("first", [1, 3, 7])
def test_a_first_attempt_id_not_ending_in_zero_publishes_first_and_repairs_over_that_head(
    ledger_service: LedgerService, tmp_path: Path, first: int
) -> None:
    ready = _isolated_ready_to_open(
        ledger_service, tmp_path, attempt_id=f"issue-142-attempt-{first}"
    )

    ready.open()

    (request,) = ready.publisher.requests
    assert request.expected_remote_revision is None, (
        "the first publication expects no remote branch"
    )
    assert ready.remote_branch() == ready.head
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None and open_pr["attempt_id"] == f"open-pr-{first}"
    assert _next_action(ready) is Stage1ProgressionAction.WAIT_FOR_CI

    repaired = _advance_to_repair_generation(ready, generation=first + 1)
    ready.open()

    _, second = ready.publisher.requests
    assert second.head_revision == repaired
    assert second.expected_remote_revision == ready.head, (
        "the previous generation is the real first attempt, not a derived -implement-N id"
    )
    assert ready.remote_branch() == repaired
    open_pr = _node(ledger_service, "open_pr")
    assert open_pr is not None and open_pr["attempt_id"] == f"open-pr-{first + 1}"


def test_a_review_for_another_repair_index_than_the_implementations_is_refused(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    ready = _isolated_ready_to_open(ledger_service, tmp_path, attempt_id="issue-142-attempt-3")

    with pytest.raises(InvalidInputError, match="repair index"):
        ready.built.service.review_implementation(
            ready.built.request,
            ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
            repair_attempt=3,
            now=NOW + timedelta(seconds=5),
            heartbeat_window=HEARTBEAT,
        )


# --- isolated validation and the tools the image has ------------------------------------------


@dataclass
class ToolRunner:
    """An isolated runner whose command result and ``command -v`` probe answer are scripted."""

    returncode: int
    output: str = "output\n"
    tool_present: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(
        self, command: tuple[str, ...], workspace: Path
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[:2] == ("sh", "-c"):
            return subprocess.CompletedProcess(command, 0 if self.tool_present else 1, "", "")
        return subprocess.CompletedProcess(command, self.returncode, self.output, "")


def _ready_to_validate_with(
    ledger: LedgerService, tmp_path: Path, runner: ToolRunner, *, command: tuple[str, ...]
) -> Harnessed:
    built = _build(
        ledger,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=cast(RecordingIsolatedRunner, runner),
        validation_commands=(command,),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    _verify_implementation(built)
    return built


@pytest.mark.parametrize(
    ("returncode", "output"),
    [
        (127, "sh: 1: pytest: not found\n"),
        (1, "[Errno 2] No such file or directory: 'pytest'"),
        (1, "[Errno 13] Permission denied: 'pytest'"),
    ],
    ids=["exit-127", "wrapper-enoent", "wrapper-eacces"],
)
def test_a_validation_tool_missing_from_the_image_is_a_hold_naming_it_not_a_failed_validation(
    ledger_service: LedgerService, tmp_path: Path, returncode: int, output: str
) -> None:
    runner = ToolRunner(returncode=returncode, output=output, tool_present=False)
    built = _ready_to_validate_with(
        ledger_service, tmp_path, runner, command=("pytest", "-q", "tests")
    )

    with pytest.raises(HumanActionRequiredError) as caught:
        _validate(built)

    assert "'pytest'" in str(caught.value)
    assert "not available in the pinned runner image" in str(caught.value)
    assert caught.value.details["tool"] == "pytest"
    assert runner.calls[0] == ("pytest", "-q", "tests")
    assert runner.calls[1][:2] == ("sh", "-c") and runner.calls[1][-1] == "pytest", (
        "the same container restrictions are asked whether the executable exists"
    )
    node = _node(ledger_service, "validate")
    assert node is not None
    assert node["status"] == "awaiting_human", "a hold, never a recorded verdict"
    assert node["tool"] == "pytest"
    assert "'pytest'" in str(node["human_wait_reason"])
    assert "validation_artifact_digest" not in node
    assert not validation_path_for(built.request.workspace_path).exists()
    action = built.service.next_action(built.request.run.id).action
    assert action is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW


def test_exit_127_from_a_tool_that_exists_is_an_ordinary_failed_validation(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    runner = ToolRunner(returncode=127, output="script exited 127 itself\n", tool_present=True)
    built = _ready_to_validate_with(ledger_service, tmp_path, runner, command=("python3", "x.py"))

    result = _validate(built)

    assert result.passed is False
    node = _node(ledger_service, "validate")
    assert node is not None and node["status"] == "failed"
    assert len(runner.calls) == 2, "the probe ran once and found the tool"


@pytest.mark.parametrize(
    ("returncode", "output", "passed"),
    [
        (1, "3 tests failed\n", False),
        (2, "[Errno 2] only mentioned in passing\n", False),
        (0, "ok\n", True),
    ],
    ids=["failure", "other-exit-code", "pass"],
)
def test_an_ordinary_result_is_never_probed(
    ledger_service: LedgerService, tmp_path: Path, returncode: int, output: str, passed: bool
) -> None:
    runner = ToolRunner(returncode=returncode, output=output, tool_present=False)
    built = _ready_to_validate_with(ledger_service, tmp_path, runner, command=("make",))

    assert _validate(built).passed is passed

    assert runner.calls == [("make",)]


def test_an_isolated_run_cannot_start_with_the_host_default_validation_command(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        validation_commands=(DEFAULT_VALIDATION_COMMAND,),
    )

    with pytest.raises(InvalidInputError, match="must name its validation commands"):
        built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None


def test_a_trusted_local_run_keeps_the_host_default_validation_command(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=RunnerSelection.trusted_local(),
        validation_commands=(DEFAULT_VALIDATION_COMMAND,),
    )

    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is not None
