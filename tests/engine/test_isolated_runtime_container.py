"""Opt-in: one isolated implementation attempt through the runtime on a real container engine.

Runs only when ``ENGINERY_RUNNER_IMAGE`` names the exact id of a built runner image (see
``enginery runner build-image``) and the container engine reachable through the default docker
context can enforce the isolated profile. It exercises the full host path with nothing replaced:
the coordinator runtime materializes a self-contained clone, supervises the real launcher
process, the launcher starts the hardened container, a command inside it commits on a branch and
the worker entry bundles it, and the host verifies the branch from a host-owned intake
repository and removes everything, including files the container created.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.adapters.oci.engine import EngineCommandError, OciEngine
from enginery.adapters.oci.image import verify_image
from enginery.adapters.runner_launch import (
    isolated_command,
    runner_container_name,
    runner_receipt_path,
    worker_environment,
)
from enginery.domain.errors import MissingPrerequisiteError
from enginery.domain.runner import (
    ResourceLimits,
    RunnerContext,
    RunnerProfile,
    RunnerReceipt,
    RunnerSelection,
    isolated_permission_mapping,
    profile_violations,
)
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.engine.task_repository import (
    BUNDLE_FILE_NAME,
    intake_path_for,
    out_path_for,
)
from enginery.engine.workspace import WORKSPACE_MODE_TASK_LOCAL_CLONE, GitWorktreeBackend
from enginery.ledger.service import LedgerService
from tests.support.runner_context import harness_task

_IMAGE_VARIABLE = "ENGINERY_RUNNER_IMAGE"
_BRANCH = "tb"
_NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
_WORKER_SCRIPT = (
    "git checkout -q -b tb && printf 'implemented\\n' > feature.txt "
    "&& git add feature.txt && git commit -q -m 'implement in container'"
)


def _engine_and_image() -> tuple[OciEngine, str]:
    image_id = os.environ.get(_IMAGE_VARIABLE, "").strip()
    if not image_id:
        pytest.skip(f"{_IMAGE_VARIABLE} is not set; the real-container test is opt-in")
    engine = OciEngine()
    try:
        engine.qualify()
    except MissingPrerequisiteError as error:
        pytest.skip(f"no qualified container engine is reachable (docker info): {error}")
    verify_image(engine, image_id)  # a configured image that is stale or absent must fail loudly
    return engine, image_id


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_a_container_commits_on_a_branch_and_the_host_verifies_and_cleans_it(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    engine, image_id = _engine_and_image()
    root = tmp_path.resolve()  # the engine's file sharing needs a symlink-free path
    repository = root / "repository"
    repository.mkdir()
    _git("init", "-q", cwd=repository)
    _git("config", "user.email", "host@example.invalid", cwd=repository)
    _git("config", "user.name", "Host", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-q", "-m", "fixture", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)

    network = f"enginery-test-{uuid.uuid4().hex[:10]}"
    profile = RunnerProfile(
        network=network,
        resources=ResourceLimits(
            memory_mib=256, cpus=1.0, pids=128, tmp_mib=32, home_mib=32, stop_grace_seconds=2
        ),
    )
    receipts = root / "receipts"
    receipts.mkdir()
    context = RunnerContext(
        selection=RunnerSelection.isolated(image=image_id, profile=profile),
        profile=profile,
        receipt_root=str(receipts),
    )
    operation_id = f"test-{uuid.uuid4().hex[:12]}"
    container = runner_container_name(operation_id)
    workspace = root / "workspaces" / "run-1"
    task = harness_task(
        workspace,
        "omp",
        operation_id=operation_id,
        head_branch=_BRANCH,
        base_revision=base,
    )
    permissions = isolated_permission_mapping("omp")
    command = isolated_command(
        runner=context,
        harness="omp",
        task=task,
        result_path=out_path_for(workspace) / "result.json",
        permissions=permissions,
        environment=worker_environment(()),
        # The launcher refuses an inner command that does not carry the recorded permission
        # arguments; the stand-in worker script receives them as ignored positional parameters.
        inner_command=("sh", "-c", _WORKER_SCRIPT, "sh", *permissions.arguments),
    )
    logs = root / "logs"
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    try:
        ticked = runtime.tick(
            now=_NOW,
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=30),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
            requests=(
                FixtureDispatch(
                    run_id="run-1",
                    node_id="implement",
                    attempt_id="attempt-1",
                    repository_id="repository-1",
                    repository_path=repository,
                    workspace_path=workspace,
                    base_revision=base,
                    command=command,
                    expected_attempt_version=0,
                    operation_id=operation_id,
                    log_directory=logs,
                    workspace_mode=WORKSPACE_MODE_TASK_LOCAL_CLONE,
                ),
            ),
        )
        assert len(ticked.dispatched) == 1
        dispatched = ticked.dispatched[0]
        assert (workspace / ".git").is_dir()

        _, status = os.waitpid(dispatched.identity.pid, 0)
        stderr_log = logs / "run-1-implement-attempt-1.stderr.log"
        launcher_stderr = stderr_log.read_text(encoding="utf-8") if stderr_log.exists() else ""
        assert os.waitstatus_to_exitcode(status) == 0, launcher_stderr

        # The runner receipt records what the engine actually held, and satisfies the profile.
        receipt_path = Path(runner_receipt_path(context, operation_id))
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt = RunnerReceipt.from_state(raw)
        assert receipt.to_state() == raw
        assert receipt.operation_id == operation_id
        assert receipt.container_name == container
        assert receipt.image.image_id == image_id
        assert receipt.profile_digest == profile.digest
        assert profile_violations(profile, receipt.effective) == ()
        # The worker's own result and its bundle are in the out directory, not the workspace.
        result = json.loads((out_path_for(workspace) / "result.json").read_text(encoding="utf-8"))
        assert result["terminal_status"] == "succeeded"
        bundle = out_path_for(workspace) / BUNDLE_FILE_NAME
        assert bundle.is_file()
        heads = _git("bundle", "list-heads", str(bundle), f"refs/heads/{_BRANCH}", cwd=root)
        bundled_head = heads.split()[0]

        verified = runtime.verify_implementation_branch(run_id="run-1", head_branch=_BRANCH)

        assert verified == bundled_head
        assert verified != base
        intake = intake_path_for(workspace)
        assert _git("--git-dir", str(intake), "rev-parse", f"{verified}^", cwd=root) == base
        assert (
            _git("--git-dir", str(intake), "show", f"{verified}:feature.txt", cwd=root)
            == "implemented"
        )
        assert _git("branch", "--list", _BRANCH, cwd=repository) == ""

        # The container is gone, and the workspace never learned where the host repository is.
        assert engine.container_exists(container) is False
        listed = engine.run_cli(
            ("ps", "-a", "--filter", f"name=^{container}$", "--format", "{{.Names}}")
        )
        assert listed.returncode == 0 and listed.stdout.strip() == ""
        host_paths = {str(repository).encode(), str(repository.resolve()).encode()}
        for entry in sorted((workspace / ".git").rglob("*")):
            if (
                entry.relative_to(workspace / ".git").parts[0] in {"objects", "refs"}
                or not entry.is_file()
                or entry.is_symlink()
            ):
                continue
            data = entry.read_bytes()
            assert not any(path in data for path in host_paths), entry

        # Everything the container created (as another uid) is removable by the host user.
        backend = GitWorktreeBackend(ledger_service, runtime.coordinator)
        epoch = runtime.coordinator.current_epoch()
        assert epoch is not None
        cleaned = backend.cleanup(
            dispatched.workspace, epoch=epoch.epoch, now=_NOW + timedelta(seconds=30)
        )
        assert cleaned.status == "cleaned"
        assert not workspace.exists()
        assert not out_path_for(workspace).exists()
        assert not intake.exists()
    finally:
        with contextlib.suppress(EngineCommandError):
            engine.remove(container)
        engine.run_cli(("network", "rm", network))
