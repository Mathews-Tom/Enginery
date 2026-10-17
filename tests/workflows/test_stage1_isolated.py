"""Stage 1 runs bound to the isolated runner: binding, admission holds and mode-specific dispatch.

A run bound to the isolated profile is never run in another mode. These tests drive the real
``Stage1RunService`` over a real ledger and a real git repository, and assert on durable state
and on observable host effects (markers a host-run command would leave), not on wiring.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from tests.adapters.oci.support import IMAGE_ID as FAKE_IMAGE_ID
from tests.adapters.oci.support import FakeDocker
from tests.engine.test_isolated_workspace import _WORKER as WORKER_SCRIPT
from tests.support.behavioral import fixture_behavioral_checks
from tests.support.execution import selection_for
from tests.support.verified_implementation import verified_head

from enginery.adapters.oci import image as oci_image
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.application.adapter_types import NormalizedAdapterEvent
from enginery.application.runner_ports import RunnerAdmissionDecision
from enginery.application.work_ports import (
    HarnessResult,
    HarnessTask,
    WorkLedgerPort,
    WorkLedgerSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import HumanActionRequiredError, MissingPrerequisiteError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.execution_selection import ExecutionHold, HoldReason
from enginery.domain.ids import NodeId, OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.runner import RunnerProfile, RunnerSelection
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch, WorkflowNodeDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.task_repository import intake_path_for, out_path_for, validation_path_for
from enginery.engine.workspace import WORKSPACE_MODE_GIT_WORKTREE, WORKSPACE_MODE_TASK_LOCAL_CLONE
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationDispatch,
    Stage1ImplementationRequest,
    Stage1ProgressionAction,
    Stage1RunRequest,
    Stage1RunService,
    stage1_request_from_state,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
HEARTBEAT = timedelta(seconds=60)
LEASE = timedelta(seconds=30)
LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
IMAGE = "sha256:" + "a" * 64
RUN_ID = "run-isolated"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git("init", "-q", cwd=repository)
    _git("config", "user.email", "enginery@example.test", cwd=repository)
    _git("config", "user.name", "Enginery Test", cwd=repository)
    (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repository)
    _git("commit", "-q", "-m", "baseline", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _snapshot() -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("work-1"),
            work_kind=WorkKind.ISSUE,
            source_provider="github",
            external_reference="issue:1",
            source_snapshot_reference="issue:1@1",
            title="Bounded change",
            objective="Change one bounded behavior.",
            acceptance_criteria=("observable result",),
            constraints=("retain evidence",),
            risk_class=RiskClass.LOW,
            repository_targets=("repository-1",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="1",
    )


class StaticWorkLedger:
    def __init__(self, snapshot: WorkLedgerSnapshot) -> None:
        self.snapshot = snapshot

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        assert external_reference == "issue:1"
        return self.snapshot


class ScriptedAdmission:
    """An admission port answering with a settable hold, recording what it was asked about."""

    def __init__(self, hold: ExecutionHold | None = None) -> None:
        self.hold = hold
        self.selections: list[RunnerSelection] = []

    def admit(self, selection: RunnerSelection) -> RunnerAdmissionDecision:
        self.selections.append(selection)
        return RunnerAdmissionDecision(hold=self.hold)


@dataclass
class RecordingHarness:
    """A supervised-harness stand-in whose worker is a trivial local command.

    ``installed`` is what its in-image probe reports: by default exactly the catalog's omp.
    """

    artifact_store: ArtifactStore
    command: tuple[str, ...] = (sys.executable, "-c", "pass")
    installed: InstalledHost | Exception | None = field(
        default_factory=lambda: InstalledHost(
            executable_version="18.10.3", protocol_fingerprint="json-protocol=3"
        )
    )
    probes: int = 0
    result_paths: list[Path] = field(default_factory=list)

    def supervised_command(self, task: HarnessTask, *, result_path: Path) -> tuple[str, ...]:
        self.result_paths.append(result_path)
        return self.command

    def installed_host(self) -> InstalledHost | None:
        self.probes += 1
        if isinstance(self.installed, Exception):
            raise self.installed
        return self.installed

    def collect_supervised(
        self,
        task: HarnessTask,
        *,
        result_path: Path,
        launched_command: tuple[str, ...] | None = None,
    ) -> tuple[tuple[NormalizedAdapterEvent, ...], HarnessResult]:
        raise AssertionError("collection is not exercised here")


class BundlingHarness(RecordingHarness):
    """Plays the in-container worker: bundles its branch on the base the task carries."""

    def supervised_command(self, task: HarnessTask, *, result_path: Path) -> tuple[str, ...]:
        super().supervised_command(task, result_path=result_path)
        assert task.base_revision is not None and task.head_branch is not None
        return (
            sys.executable,
            "-c",
            WORKER_SCRIPT,
            task.base_revision,
            task.head_branch,
            str(result_path.parent / "marker"),
            "implement",
        )


class RecordingIsolatedRunner:
    """The isolated validation runner: records every call and reports a fixed exit code."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(
        self, command: tuple[str, ...], workspace: Path
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, workspace))
        return subprocess.CompletedProcess(command, self.returncode, "isolated output\n", "")


@dataclass
class Harnessed:
    request: Stage1RunRequest
    service: Stage1RunService
    runtime: CoordinatorRuntime
    ledger: LedgerService
    harness: RecordingHarness
    repository: Path


def _request(
    tmp_path: Path,
    *,
    runner: RunnerSelection,
    base_revision: str,
    repository: Path,
    validation_commands: tuple[tuple[str, ...], ...] = (("python3", "-m", "unittest"),),
    deterministic_implement: bool = False,
    attempt_id: str = "implement-0",
) -> Stage1RunRequest:
    template = stage1_work_manifest()
    manifest = template
    if deterministic_implement:
        manifest = replace(
            template,
            nodes={
                **template.nodes,
                NodeId("implement"): replace(
                    template.nodes[NodeId("implement")], actor_type=ActorType.DETERMINISTIC
                ),
            },
        )
    snapshot = _snapshot()
    return Stage1RunRequest(
        behavioral_checks=fixture_behavioral_checks(),
        runner=runner,
        run=Run(
            id=RunId(RUN_ID),
            work_item_id=snapshot.work_item.id,
            work_item_snapshot_digest=snapshot.work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="repository-1",
            base_revision=base_revision,
            policy_set_version="policy-v1",
            adapter_versions={},
            adapter_fingerprints={},
            capability_lock_digest=Digest.of_bytes(b"capability-lock"),
            environment_manifest_digest=Digest.of_bytes(b"environment"),
            configuration_snapshot_digest=Digest.of_bytes(b"configuration"),
            state=RunState.CREATED,
        ),
        work_snapshot=snapshot,
        manifest=manifest,
        repository_id="repository-1",
        repository_path=repository,
        execution_selection=selection_for("omp"),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/Enginery",
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            harness_provider="omp",
            harness_credential_reference="test-omp-keyring",
            harness_executable="omp",
            artifact_root=(tmp_path / "artifacts").resolve(),
        ),
        workspace_path=(tmp_path / "workspaces" / "run").resolve(),
        base_branch="main",
        head_branch="enginery/run-isolated",
        validation_commands=validation_commands,
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id=attempt_id,
            operation_id=OperationId("implement:run-isolated"),
            time_budget_seconds=60,
            cost_budget=None,
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
    )


def _isolated() -> RunnerSelection:
    return RunnerSelection.isolated(image=IMAGE, profile=RunnerProfile())


def _build(
    ledger: LedgerService,
    tmp_path: Path,
    *,
    runner: RunnerSelection,
    admission: ScriptedAdmission | OciRunnerAdmission | None = None,
    isolated_runner: RecordingIsolatedRunner | None = None,
    validation_commands: tuple[tuple[str, ...], ...] = (("python3", "-m", "unittest"),),
    deterministic_implement: bool = False,
    symbolic_base: str | None = None,
    harness: RecordingHarness | None = None,
    attempt_id: str = "implement-0",
) -> Harnessed:
    repository, base = _repository(tmp_path)
    if symbolic_base is not None:
        _git("branch", "-M", symbolic_base, cwd=repository)
        base = symbolic_base
    request = _request(
        tmp_path,
        runner=runner,
        base_revision=base,
        repository=repository,
        validation_commands=validation_commands,
        deterministic_implement=deterministic_implement,
        attempt_id=attempt_id,
    )
    runtime = CoordinatorRuntime(ledger, owner="coordinator")
    harness = harness or RecordingHarness(ArtifactStore(tmp_path / "artifacts"))
    service = Stage1RunService(
        behavioral_verifier=lambda request, head: Digest.of_bytes(b"fixture behavioral verdict"),
        runtime=runtime,
        ledger=ledger,
        work_ledger=cast(WorkLedgerPort, StaticWorkLedger(_snapshot())),
        harness=harness,
        runner_admission=admission,
        isolated_command_runner=isolated_runner,
    )
    return Harnessed(request, service, runtime, ledger, harness, repository)


def _qualify(built: Harnessed) -> None:
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    progression = built.service.advance(
        built.request.run.id,
        now=NOW + timedelta(seconds=1),
        heartbeat_window=HEARTBEAT,
        lease_window=LEASE,
        limits=LIMITS,
    )
    assert progression.action is Stage1ProgressionAction.IMPLEMENT


def _dispatch(built: Harnessed, *, offset: int = 2) -> Stage1ImplementationDispatch:
    return built.service.dispatch_implementation(
        built.request,
        now=NOW + timedelta(seconds=offset),
        heartbeat_window=HEARTBEAT,
        lease_window=LEASE,
        limits=LIMITS,
    )


def _unqualified_hold() -> ExecutionHold:
    return ExecutionHold(
        reason=HoldReason.RUNNER_UNQUALIFIED,
        message="no qualified container engine is available; the run is held",
    )


def _labelled_fake_docker() -> FakeDocker:
    fake = FakeDocker()
    fake.images[FAKE_IMAGE_ID]["Config"]["Labels"] = {
        oci_image.RECIPE_DIGEST_LABEL: oci_image.recipe_digest(),
        oci_image.RUNNER_IMAGE_LABEL: "1",
        oci_image.CLAUDE_CODE_LABEL: "2.1.296",
        oci_image.OMP_LABEL: "18.10.3",
        oci_image.BUN_LABEL: "1.3.14",
    }
    return fake


def _real_admission(fake: FakeDocker) -> OciRunnerAdmission:
    def factory(context: str | None) -> OciEngine:
        return fake.engine(context=context)

    return OciRunnerAdmission(engine_factory=factory)


def _reap(dispatch: Stage1ImplementationDispatch) -> None:
    fixture = dispatch.fixture
    assert fixture is not None
    # The supervisor may already have reaped the worker; the exit is observable either way.
    with contextlib.suppress(ChildProcessError):
        os.waitpid(fixture.identity.pid, 0)


# --- the request binds the trust mode --------------------------------------------------


def test_the_isolated_binding_is_part_of_the_request_state_and_digest(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    repository, base = _repository(tmp_path)
    isolated = _request(tmp_path, runner=_isolated(), base_revision=base, repository=repository)
    trusted = replace(isolated, runner=RunnerSelection.trusted_local())

    state = isolated.initial_state()

    runner_state = state["runner"]
    assert isinstance(runner_state, dict)
    assert runner_state["image"] == IMAGE
    assert runner_state["profile_digest"] == str(RunnerProfile().digest)
    assert isolated.digest != trusted.digest
    assert state["request_digest"] == str(isolated.digest)
    assert stage1_request_from_state(state) == isolated
    # An explicit trusted-local choice is recorded too; only a pre-trust-mode run has no key.
    assert "runner" in trusted.initial_state()
    legacy = replace(isolated, runner=replace(RunnerSelection.trusted_local(), legacy=True))
    assert "runner" not in legacy.initial_state()
    assert legacy.digest not in {isolated.digest, trusted.digest}


def test_a_different_pinned_image_is_a_different_run_binding(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    one = _request(tmp_path, runner=_isolated(), base_revision=base, repository=repository)
    other = replace(
        one, runner=RunnerSelection.isolated(image="sha256:" + "b" * 64, profile=RunnerProfile())
    )

    assert one.digest != other.digest


# --- admission gates the run ------------------------------------------------------------


def test_a_run_whose_admission_holds_is_not_started_in_any_mode(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    hold = _unqualified_hold()
    admission = ScriptedAdmission(hold)
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=admission)

    with pytest.raises(HumanActionRequiredError, match="no qualified container engine"):
        built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert admission.selections == [built.request.runner]
    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None, (
        "a held run leaves no run projection"
    )
    assert ledger_service.list_projections(aggregate_type="runtime_node") == ()
    assert not built.request.workspace_path.exists()


def test_a_run_whose_admission_reports_drift_is_refused_with_the_drift_details(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    hold = ExecutionHold(
        reason=HoldReason.RUNNER_FINGERPRINT_DRIFT,
        message="the pinned worker image was not built from the current runner recipe",
        details={"recorded": "sha256:old", "expected": "sha256:new"},
    )
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=ScriptedAdmission(hold))

    with pytest.raises(HumanActionRequiredError) as caught:
        built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert caught.value.details == {"recorded": "sha256:old", "expected": "sha256:new"}
    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None


def test_an_isolated_run_with_no_admission_configured_is_held_not_run_locally(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=None)

    with pytest.raises(HumanActionRequiredError, match="no runner admission is configured"):
        built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None


def _started_isolated(ledger_service: LedgerService, tmp_path: Path) -> Harnessed:
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=ScriptedAdmission())
    assert not built.request.execution_selection.requires_native_controls  # a host default
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    return built


@pytest.mark.parametrize(
    ("reported", "observed_version", "observed_protocol"),
    [
        (InstalledHost("18.10.4", "json-protocol=3"), "18.10.4", "json-protocol=3"),
        (InstalledHost("18.10.3", "json-protocol=2"), "18.10.3", "json-protocol=2"),
        (None, "unobservable", "unobservable"),
    ],
)
def test_an_isolated_host_default_run_holds_when_the_image_harness_drifts_from_the_catalog(
    ledger_service: LedgerService,
    tmp_path: Path,
    reported: InstalledHost | None,
    observed_version: str,
    observed_protocol: str,
) -> None:
    built = _started_isolated(ledger_service, tmp_path)
    built.harness.installed = reported

    progression = built.service.next_action(built.request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    hold = progression.hold
    assert hold is not None
    assert hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT
    assert hold.details == {
        "harness": "omp",
        "accepted_executable_versions": ">=18.10.3,<19.0.0",
        "tested_executable_version": "18.10.3",
        "catalog_protocol_fingerprint": "json-protocol=3",
        "image_executable_version": observed_version,
        "image_protocol_fingerprint": observed_protocol,
    }
    with pytest.raises(HumanActionRequiredError, match="inside the pinned runner image"):
        _dispatch(built)
    assert built.harness.result_paths == []  # nothing was launched


def test_an_isolated_host_default_run_is_probed_and_proceeds_when_the_image_matches(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _started_isolated(ledger_service, tmp_path)

    progression = built.service.next_action(built.request.run.id)

    assert progression.hold is None
    assert progression.action is Stage1ProgressionAction.QUALIFY
    assert built.harness.probes >= 1


def test_a_trusted_local_host_default_run_never_probes_the_harness(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    built.service.next_action(built.request.run.id)

    assert built.harness.probes == 0


def test_an_image_harness_that_cannot_be_probed_holds_as_unqualified(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _started_isolated(ledger_service, tmp_path)
    built.harness.installed = MissingPrerequisiteError("the probe container could not be started")

    progression = built.service.next_action(built.request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "could not be probed" in progression.hold.message


def test_an_isolated_run_with_no_way_to_probe_the_image_harness_is_held(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _started_isolated(ledger_service, tmp_path)
    unprobed = replace(built.service, harness=None, host_probe=None)

    progression = unprobed.next_action(built.request.run.id)

    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "cannot be probed" in progression.hold.message


def test_a_run_that_binds_no_trust_mode_cannot_start(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    legacy = replace(RunnerSelection.trusted_local(), legacy=True)
    built = _build(ledger_service, tmp_path, runner=legacy)

    with pytest.raises(Exception, match="must bind a trust mode"):
        built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None


def test_a_passing_admission_starts_the_run_and_a_trusted_run_never_consults_admission(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    admission = ScriptedAdmission()
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=admission)

    run = built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert run.request == built.request
    assert admission.selections == [built.request.runner]
    stored = ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID)
    assert stored is not None
    assert stored.state["request_digest"] == str(built.request.digest)
    progression = built.service.next_action(built.request.run.id)
    assert progression.action is Stage1ProgressionAction.QUALIFY
    assert progression.hold is None

    trusted_admission = ScriptedAdmission(_unqualified_hold())
    trusted = _build(
        ledger_service,
        tmp_path / "trusted",
        runner=RunnerSelection.trusted_local(),
        admission=trusted_admission,
    )
    trusted.service.start(
        replace(trusted.request, run=replace(trusted.request.run, id=RunId("run-trusted"))),
        now=NOW,
        heartbeat_window=HEARTBEAT,
    )
    assert trusted_admission.selections == []


def test_a_started_run_whose_engine_later_disappears_holds_and_launches_nothing(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    fake = _labelled_fake_docker()
    built = _build(
        ledger_service, tmp_path, runner=_isolated_for_fake_image(), admission=_real_admission(fake)
    )
    _qualify(built)
    assert built.service.next_action(built.request.run.id).hold is None

    fake.unreachable = True
    progression = built.service.next_action(built.request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "unreachable" in progression.hold.message
    with pytest.raises(HumanActionRequiredError, match="unreachable"):
        _dispatch(built)
    # Nothing was launched: no node, no lease, no reservation, no workspace.
    assert (
        ledger_service.read_projection(
            aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:implement"
        )
        is None
    )
    assert ledger_service.read_lease(run_id=RUN_ID, node_id="implement") is None
    assert (
        ledger_service.read_process_manager_state(
            process_manager_name="workspace-reservations", state_key="repository-1"
        )
        is None
    )
    assert not built.request.workspace_path.exists()
    assert built.harness.result_paths == []

    fake.unreachable = False
    assert (
        built.service.next_action(built.request.run.id).action is Stage1ProgressionAction.IMPLEMENT
    )


def _isolated_for_fake_image() -> RunnerSelection:
    return RunnerSelection.isolated(image=FAKE_IMAGE_ID, profile=RunnerProfile())


def test_a_run_bound_to_another_profile_holds_with_fingerprint_drift(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    fake = _labelled_fake_docker()
    bound = RunnerSelection.isolated(
        image=FAKE_IMAGE_ID, profile=RunnerProfile(network="the-network-it-was-admitted-under")
    )
    built = _build(ledger_service, tmp_path, runner=bound, admission=_real_admission(fake))

    with pytest.raises(HumanActionRequiredError, match="not the current one"):
        built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)

    assert fake.calls == []
    assert ledger_service.read_projection(aggregate_type="run", aggregate_id=RUN_ID) is None


def test_profile_drift_after_start_is_reported_as_fingerprint_drift_hold(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    fake = _labelled_fake_docker()
    admission = ScriptedAdmission()
    built = _build(ledger_service, tmp_path, runner=_isolated_for_fake_image(), admission=admission)
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    # The installation now ships a different runner profile than the one the run was bound to.
    stricter = RunnerProfile(network="a-newer-runner-network")
    drifted = replace(built.service, runner_admission=replace_admission_profile(fake, stricter))

    progression = drifted.next_action(built.request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT
    assert progression.hold.details["bound"] == str(RunnerProfile().digest)
    assert progression.hold.details["current"] == str(stricter.digest)


def replace_admission_profile(fake: FakeDocker, profile: RunnerProfile) -> OciRunnerAdmission:
    return OciRunnerAdmission(
        engine_factory=lambda context: fake.engine(context=context), profile=profile
    )


# --- implementation dispatch ------------------------------------------------------------


def test_an_isolated_worker_is_told_to_commit_locally_and_never_to_push(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=_isolated(), admission=ScriptedAdmission())
    _qualify(built)

    dispatch = _dispatch(built)
    _reap(dispatch)

    task = dispatch.task
    constraint = task.constraints[-1]
    assert "commit the completed change locally" in constraint
    assert "do not push" in constraint
    assert "push the completed change to origin" not in constraint
    assert task.head_branch == "enginery/run-isolated"
    assert task.base_revision == _git("rev-parse", "HEAD", cwd=built.repository)
    assert task.base_revision in constraint


def test_a_symbolic_base_reaches_the_worker_as_a_full_object_id_and_the_bundle_verifies(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    harness = BundlingHarness(ArtifactStore(tmp_path / "artifacts"))
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        symbolic_base="main",
        harness=harness,
    )
    assert built.request.run.base_revision == "main"
    oid = _git("rev-parse", "main", cwd=built.repository)
    _qualify(built)

    dispatch = _dispatch(built)
    _reap(dispatch)

    assert dispatch.task.base_revision == oid
    assert "'main'" not in dispatch.task.constraints[-1]
    assert oid in dispatch.task.constraints[-1]
    head = built.runtime.verify_implementation_branch(
        run_id=RUN_ID, head_branch=built.request.head_branch
    )
    intake = intake_path_for(built.request.workspace_path)
    assert _git("--git-dir", str(intake), "rev-parse", f"{head}^", cwd=tmp_path) == oid


def test_a_later_attempt_reuses_the_first_resolved_base_after_the_base_name_moves(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        symbolic_base="main",
    )
    first = _git("rev-parse", "main", cwd=built.repository)
    _qualify(built)
    dispatch = _dispatch(built)
    _reap(dispatch)
    assert dispatch.task.base_revision == first
    (built.repository / "tracked.txt").write_text("moved\n", encoding="utf-8")
    _git("commit", "-q", "-am", "the base name moves", cwd=built.repository)
    assert _git("rev-parse", "main", cwd=built.repository) != first
    # The reservation is gone by the time a later generation is dispatched or collected.
    monkeypatch.setattr(built.runtime, "read_workspace_reservation", lambda repository_id: None)

    assert built.service._implementation_base(built.request) == first


def test_a_trusted_local_worker_is_told_the_same_it_commits_locally_and_never_pushes(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    built = _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())
    _qualify(built)

    dispatch = _dispatch(built)
    _reap(dispatch)

    task = dispatch.task
    constraint = task.constraints[-1]
    assert "commit the completed change locally" in constraint
    assert "do not push" in constraint
    assert "push the completed change to origin" not in constraint
    assert task.head_branch is None, "no bundle binding: the trusted worker is not bundled"
    assert task.base_revision is None


def test_the_isolated_result_path_is_in_the_out_directory_and_trusted_local_stays_in_the_workspace(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    isolated = _build(
        ledger_service, tmp_path / "iso", runner=_isolated(), admission=ScriptedAdmission()
    )
    _qualify(isolated)
    isolated_dispatch = _dispatch(isolated)
    _reap(isolated_dispatch)

    result_path = isolated_dispatch.result_path
    assert result_path.parent == out_path_for(isolated.request.workspace_path)
    assert not result_path.is_relative_to(isolated.request.workspace_path)
    assert isolated.harness.result_paths == [result_path]

    ledger_two = tmp_path / "trusted-ledger"
    ledger_two.mkdir()
    with LedgerService.open(ledger_two / "ledger.sqlite3") as other:
        trusted = _build(other, tmp_path / "trusted", runner=RunnerSelection.trusted_local())
        _qualify(trusted)
        trusted_dispatch = _dispatch(trusted)
        _reap(trusted_dispatch)
        trusted_path = trusted_dispatch.result_path
        assert trusted_path.parent == trusted.request.workspace_path


def test_the_implementation_node_persists_the_workspace_mode_of_its_trust_mode(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    isolated = _build(
        ledger_service, tmp_path / "iso", runner=_isolated(), admission=ScriptedAdmission()
    )
    _qualify(isolated)
    dispatch = _dispatch(isolated)
    _reap(dispatch)

    request = isolated.runtime.read_node_request(run_id=RUN_ID, node_id="implement")
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:implement"
    )
    assert request.workspace_mode == WORKSPACE_MODE_TASK_LOCAL_CLONE
    assert node is not None
    assert node.state["workspace_mode"] == WORKSPACE_MODE_TASK_LOCAL_CLONE
    reservation = isolated.runtime.read_workspace_reservation("repository-1")
    assert reservation is not None
    assert reservation.mode == WORKSPACE_MODE_TASK_LOCAL_CLONE
    # The worker's workspace is a self-contained clone, not a linked worktree.
    assert (isolated.request.workspace_path / ".git").is_dir()
    assert _git("remote", cwd=isolated.request.workspace_path) == ""


def test_the_trusted_local_node_keeps_the_default_workspace_mode_in_its_stored_state(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    trusted = _build(ledger_service, tmp_path, runner=RunnerSelection.trusted_local())
    _qualify(trusted)
    dispatch = _dispatch(trusted)
    _reap(dispatch)

    request = trusted.runtime.read_node_request(run_id=RUN_ID, node_id="implement")
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:implement"
    )
    assert request.workspace_mode == WORKSPACE_MODE_GIT_WORKTREE
    assert node is not None
    assert "workspace_mode" not in node.state
    reservation = trusted.runtime.read_workspace_reservation("repository-1")
    assert reservation is not None
    assert reservation.mode == WORKSPACE_MODE_GIT_WORKTREE
    assert (trusted.request.workspace_path / ".git").is_file(), "a linked worktree"


# --- validation -------------------------------------------------------------------------


def _complete_through_implement(built: Harnessed) -> None:
    """Record qualify and implement as passed, the way a finished implementation leaves them."""
    mode = (
        WORKSPACE_MODE_TASK_LOCAL_CLONE
        if built.request.runner.isolated_mode
        else WORKSPACE_MODE_GIT_WORKTREE
    )
    for node_id, previous in (("qualify", None), ("implement", "qualify")):
        dispatch = WorkflowNodeDispatch(
            FixtureDispatch(
                run_id=RUN_ID,
                node_id=node_id,
                attempt_id=(
                    built.request.implementation.attempt_id
                    if node_id == "implement"
                    else f"{node_id}-0"
                ),
                repository_id="repository-1",
                repository_path=built.repository,
                workspace_path=built.request.workspace_path,
                base_revision=built.request.run.base_revision,
                command=(node_id,),
                expected_attempt_version=0,
                operation_id=f"{node_id}:{RUN_ID}",
                dependencies=() if previous is None else ((RUN_ID, previous),),
                workflow_definition_id=built.request.manifest.id.value,
                workspace_mode=mode,
            ),
            built.request.manifest,
        )
        epoch = built.runtime.register_node(dispatch=dispatch, now=NOW, heartbeat_window=HEARTBEAT)
        built.runtime.complete_node(run_id=RUN_ID, node_id=node_id, epoch=epoch.epoch, now=NOW)


def _host_marker_command(marker: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
    )


def _verify_implementation(built: Harnessed, *, committed: Mapping[str, str] | None = None) -> str:
    """Leave what a collected isolated implementation leaves: a host-verified head, recorded.

    A real worker clone commits on the head branch and bundles it; the host imports the bundle
    into its own intake repository, verifies it there, and records the verified head on the
    implementation attempt, exactly as collection does.
    """
    head_revision = verified_head(
        repository=built.repository,
        base_revision=built.request.run.base_revision,
        workspace_path=built.request.workspace_path,
        head_branch=built.request.head_branch,
        committed=committed if committed is not None else {"feature.txt": "implemented\n"},
        leftovers={"scratch.txt": "left behind by the worker\n"},
    )
    built.ledger.append(
        AppendCommand(
            correlation_id="implementation-verified-head",
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=built.request.implementation.attempt_id,
                    expected_version=0,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={"result": {"head_revision": head_revision}},
                ),
            ),
        )
    )
    return head_revision


def test_isolated_validation_runs_only_in_the_isolated_runner_never_on_the_host(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    marker = tmp_path / "host-ran-validation"
    runner = RecordingIsolatedRunner(returncode=0)
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=runner,
        validation_commands=(_host_marker_command(marker),),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    head_revision = _verify_implementation(built)

    result = built.service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )

    checkout = validation_path_for(built.request.workspace_path)
    assert runner.calls == [(_host_marker_command(marker), checkout)]
    assert not checkout.exists(), "the verified-head checkout is removed afterwards"
    assert not marker.exists(), "validation must never execute on the host"
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:validate"
    )
    assert node is not None
    assert node.state["status"] == "passed"
    assert node.state["workspace_mode"] == WORKSPACE_MODE_TASK_LOCAL_CLONE
    assert node.state["validated_revision"] == head_revision
    assert result.passed is True


def test_a_failing_isolated_validation_is_a_failed_node_not_a_host_run(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    marker = tmp_path / "host-ran-validation"
    runner = RecordingIsolatedRunner(returncode=1)
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=runner,
        validation_commands=(_host_marker_command(marker),),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)
    _verify_implementation(built)

    result = built.service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )

    assert result.passed is False
    assert not marker.exists()
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:validate"
    )
    assert node is not None and node.state["status"] == "failed"


def test_isolated_validation_without_an_isolated_runner_needs_a_human_and_runs_nothing(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    marker = tmp_path / "host-ran-validation"
    built = _build(
        ledger_service,
        tmp_path,
        runner=_isolated(),
        admission=ScriptedAdmission(),
        isolated_runner=None,
        validation_commands=(_host_marker_command(marker),),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    _complete_through_implement(built)

    with pytest.raises(HumanActionRequiredError, match="isolated validation requires"):
        built.service.validate_implementation(
            built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
        )

    assert not marker.exists()
    assert (
        ledger_service.read_projection(
            aggregate_type="runtime_node", aggregate_id=f"{RUN_ID}:validate"
        )
        is None
    ), "no validation node is recorded when validation cannot run"


def test_trusted_local_validation_runs_on_the_host_and_ignores_an_isolated_runner(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    marker = tmp_path / "host-ran-validation"
    runner = RecordingIsolatedRunner()
    built = _build(
        ledger_service,
        tmp_path,
        runner=RunnerSelection.trusted_local(),
        isolated_runner=runner,
        validation_commands=(_host_marker_command(marker),),
        deterministic_implement=True,
    )
    built.service.start(built.request, now=NOW, heartbeat_window=HEARTBEAT)
    built.request.workspace_path.mkdir(parents=True)
    _complete_through_implement(built)

    result = built.service.validate_implementation(
        built.request, now=NOW + timedelta(seconds=3), heartbeat_window=HEARTBEAT
    )

    assert result.passed is True
    assert marker.read_text(encoding="utf-8") == "ran"
    assert runner.calls == []
