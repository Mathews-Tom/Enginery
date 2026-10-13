from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import container_name_for
from enginery.cli.operator import main
from enginery.domain.errors import HumanActionRequiredError
from enginery.domain.runner import EngineIdentity, ImageIdentity
from enginery.engine.operator_control import PAUSED_MESSAGE
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService

_IMAGE = "sha256:" + "ab" * 32
_LIMITS = SchedulingLimits(global_concurrency=2, per_repository_concurrency=2)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


@pytest.fixture
def worker_groups() -> Iterator[list[int]]:
    """Process groups of real workers, killed at teardown whatever the test did."""
    groups: list[int] = []
    yield groups
    for group in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)


def _request(
    tmp_path: Path, repository: Path, revision: str, *, seconds: float, name: str = "1"
) -> FixtureDispatch:
    return FixtureDispatch(
        run_id=f"run-{name}",
        node_id="node-1",
        attempt_id=f"attempt-{name}",
        repository_id=f"repository-{name}",
        repository_path=repository,
        workspace_path=tmp_path / f"workspace-{name}",
        base_revision=revision,
        command=(sys.executable, "-c", f"import time; time.sleep({seconds})"),
        expected_attempt_version=0,
        operation_id=f"operation-{name}",
    )


def _dispatch(
    ledger: LedgerService,
    tmp_path: Path,
    groups: list[int],
    *,
    seconds: float,
    name: str = "1",
    owner: str = "coordinator",
) -> CoordinatorRuntime:
    repository, revision = _repository(tmp_path) if name == "1" else _existing(tmp_path)
    runtime = CoordinatorRuntime(ledger, owner=owner)
    tick = runtime.tick(
        now=datetime.now(UTC),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=60),
        limits=_LIMITS,
        requests=(_request(tmp_path, repository, revision, seconds=seconds, name=name),),
    )
    assert len(tick.dispatched) == 1
    groups.append(tick.dispatched[0].identity.process_group_id)
    return runtime


def _existing(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _run(
    capsys: pytest.CaptureFixture[str], *argv: str, **seams: object
) -> tuple[int, dict[str, object], dict[str, object]]:
    code = main(list(argv), **seams)  # type: ignore[arg-type]
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else {}
    err = json.loads(captured.err) if captured.err.strip().startswith("{") else {}
    return code, out, err


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- status, pause, resume ---------------------------------------------------------------


def test_status_reports_pause_runner_workers_workspaces_and_ledger_health(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=30)

    code, report, _ = _run(capsys, "status", "--database", str(ledger_path))

    assert code == 0
    assert report["paused"] is False
    assert report["selected_runner"] is None
    assert report["running_workers"] == 1
    workers = report["workers"]
    assert isinstance(workers, list)
    assert workers[0]["run_id"] == "run-1"
    assert workers[0]["process"] == "present"
    assert workers[0]["pid"] > 0
    assert report["workspaces_by_status"] == {"materialized": 1}
    assert report["retained_workspaces"] == 0
    assert report["ledger"]["healthy"] is True  # type: ignore[index]


def test_pause_and_resume_are_durable_idempotent_and_audited(
    ledger_service: LedgerService, ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, paused, _ = _run(
        capsys, "pause", "--database", str(ledger_path), "--reason", "maintenance window"
    )
    again_code, again, _ = _run(capsys, "pause", "--database", str(ledger_path))

    assert code == again_code == 0
    assert paused["paused"] is True and paused["changed"] is True
    assert paused["reason"] == "maintenance window"
    assert again["changed"] is False
    status_code, status, _ = _run(capsys, "status", "--database", str(ledger_path))
    assert status_code == 0 and status["paused"] is True
    projection = ledger_service.read_projection(
        aggregate_type="operator_control", aggregate_id="control"
    )
    assert projection is not None and projection.aggregate_version == 1

    resume_code, resumed, _ = _run(capsys, "resume", "--database", str(ledger_path))
    _, resumed_again, _ = _run(capsys, "resume", "--database", str(ledger_path))

    assert resume_code == 0
    assert resumed["paused"] is False and resumed["changed"] is True
    assert resumed_again["changed"] is False
    projection = ledger_service.read_projection(
        aggregate_type="operator_control", aggregate_id="control"
    )
    assert projection is not None and projection.aggregate_version == 2


def test_missing_ledger_is_a_missing_prerequisite_and_is_not_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "absent.db"

    code, _, error = _run(capsys, "pause", "--database", str(database))

    assert code == 3
    assert error["error"]["failure_class"] == "missing_prerequisite"  # type: ignore[index]
    assert not database.exists()


def test_paused_dispatch_refuses_and_resume_restores_it(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, revision = _repository(tmp_path)
    runtime = CoordinatorRuntime(ledger_service, owner="coordinator")
    request = _request(tmp_path, repository, revision, seconds=30)
    _run(capsys, "pause", "--database", str(ledger_path))

    with pytest.raises(HumanActionRequiredError, match=PAUSED_MESSAGE):
        runtime.tick(
            now=datetime.now(UTC),
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=60),
            limits=_LIMITS,
            requests=(request,),
        )

    assert (
        ledger_service.read_process_manager_state(
            process_manager_name="worker-supervisor", state_key="run-1:node-1"
        )
        is None
    )
    assert not (tmp_path / "workspace-1").exists()
    _run(capsys, "resume", "--database", str(ledger_path))
    tick = runtime.tick(
        now=datetime.now(UTC),
        heartbeat_window=timedelta(seconds=60),
        lease_window=timedelta(seconds=60),
        limits=_LIMITS,
    )
    assert len(tick.dispatched) == 1
    worker_groups.append(tick.dispatched[0].identity.process_group_id)


def test_pause_leaves_a_running_worker_untouched(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=30)
    pid = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert pid is not None
    worker_pid = int(pid.state["pid"])  # type: ignore[call-overload]

    _run(capsys, "pause", "--database", str(ledger_path))

    assert _alive(worker_pid)
    record = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert record is not None and record.state["status"] == "running"


# -- drain -------------------------------------------------------------------------------


def test_drain_requires_an_explicit_mode(ledger_service: LedgerService, ledger_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["drain", "--database", str(ledger_path)])
    assert raised.value.code == 2
    with pytest.raises(SystemExit):
        main(["drain", "--database", str(ledger_path), "--stop", "--wait-seconds", "5"])


def test_drain_wait_returns_once_the_real_worker_exits(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=1.0)

    code, report, _ = _run(capsys, "drain", "--database", str(ledger_path), "--wait-seconds", "30")

    assert code == 0
    assert report["paused"] is True and report["drained"] is True
    assert report["mode"] == "wait"
    assert len(report["workers_before"]) == 1  # type: ignore[arg-type]
    assert report["remaining"] == []
    assert 0.2 < report["waited_seconds"] < 25  # type: ignore[operator]
    status_code, status, _ = _run(capsys, "status", "--database", str(ledger_path))
    assert status_code == 0 and status["running_workers"] == 0


def test_drain_wait_times_out_with_the_worker_still_running(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)

    code, report, _ = _run(capsys, "drain", "--database", str(ledger_path), "--wait-seconds", "0.6")

    assert code == 13
    assert report["drained"] is False and report["paused"] is True
    remaining = report["remaining"]
    assert isinstance(remaining, list) and remaining[0]["process"] == "present"
    assert _alive(remaining[0]["pid"])


def test_drain_stop_terminates_the_exact_process_group_and_reconciles_the_node(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)
    record = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert record is not None
    pid = int(record.state["pid"])  # type: ignore[call-overload]
    group = int(record.state["process_group_id"])  # type: ignore[call-overload]

    code, report, _ = _run(
        capsys, "drain", "--database", str(ledger_path), "--stop", "--owner", "coordinator"
    )

    assert code == 0
    assert report["drained"] is True and report["paused"] is True
    stopped = report["stopped"]
    assert isinstance(stopped, list) and stopped[0]["stopped"] is True
    assert report["container_cleanup"].startswith("skipped")  # type: ignore[union-attr]
    assert not _alive(pid)
    with pytest.raises(ProcessLookupError):
        os.killpg(group, 0)
    node = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id="run-1:node-1"
    )
    assert node is not None and node.state["status"] == "cancelled"
    supervisor = ledger_service.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key="run-1:node-1"
    )
    assert supervisor is not None and supervisor.state["status"] == "exit_reconciled"
    assert not (tmp_path / "workspace-1").exists()


def test_drain_stop_stops_every_running_worker(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60, name="2")

    code, report, _ = _run(
        capsys, "drain", "--database", str(ledger_path), "--stop", "--owner", "coordinator"
    )

    assert code == 0
    assert len(report["stopped"]) == 2  # type: ignore[arg-type]
    assert report["remaining"] == []
    assert all(not _alive(group) for group in worker_groups)


class _FakeEngine:
    def __init__(self, *, exists: bool, fail: bool = False) -> None:
        self.exists = exists
        self.fail = fail
        self.removed: list[str] = []

    def container_exists(self, name: str) -> bool:
        return self.exists

    def remove(self, name: str, *, force: bool = True) -> None:
        if self.fail:
            from enginery.domain.errors import MissingPrerequisiteError

            raise MissingPrerequisiteError("engine unreachable")
        self.removed.append(name)


def _fake_admission() -> OciRunnerAdmission:
    class _Engine:
        def qualify(self) -> EngineIdentity:
            return EngineIdentity(
                kind="docker",
                server_version="29.0.0",
                operating_system="linux",
                kernel_version="6.8",
                cgroup_version="2",
                security_options=("name=seccomp",),
                rootless=False,
                context="fake",
            )

        def memory_total_bytes(self) -> int:
            return 32 << 30

    return OciRunnerAdmission(
        engine_factory=lambda context: _Engine(),  # type: ignore[arg-type, return-value]
        verify=lambda engine, image: ImageIdentity(image_id=image, recipe_digest="recipe-digest"),
    )


def test_drain_stop_removes_the_isolated_worker_container_of_each_stopped_worker(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)
    engine = _FakeEngine(exists=True)
    selected_code, _, _ = _run(
        capsys,
        "select-runner",
        "--database",
        str(ledger_path),
        "--image",
        _IMAGE,
        "--docker-context",
        "lima-enginery",
        admission=_fake_admission(),
    )
    contexts: list[str | None] = []

    def factory(context: str | None) -> OciEngine:
        contexts.append(context)
        return engine  # type: ignore[return-value]

    code, report, _ = _run(
        capsys,
        "drain",
        "--database",
        str(ledger_path),
        "--stop",
        "--owner",
        "coordinator",
        engine_factory=factory,
    )

    assert selected_code == 0 and code == 0
    assert report["container_cleanup"] == "performed"
    assert engine.removed == [container_name_for("operation-1")]
    assert contexts == ["lima-enginery"]
    stopped = report["stopped"]
    assert isinstance(stopped, list)
    assert stopped[0]["containers_removed"] == [container_name_for("operation-1")]


def test_drain_stop_fails_loudly_when_a_container_cannot_be_removed(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)

    code, report, _ = _run(
        capsys,
        "drain",
        "--database",
        str(ledger_path),
        "--stop",
        "--owner",
        "coordinator",
        "--docker-context",
        "ctx",
        engine_factory=lambda context: _FakeEngine(exists=True, fail=True),  # type: ignore[arg-type, return-value]
    )

    assert code == 5
    stopped = report["stopped"]
    assert isinstance(stopped, list)
    assert stopped[0]["stopped"] is True
    assert stopped[0]["container_error"] == "engine unreachable"


def test_drain_stop_reports_a_worker_it_cannot_stop(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)

    # another coordinator owns the active epoch, so the operator may not fence a cancel
    code, report, _ = _run(
        capsys, "drain", "--database", str(ledger_path), "--stop", "--owner", "someone-else"
    )

    assert code == 5
    stopped = report["stopped"]
    assert isinstance(stopped, list)
    assert stopped[0]["stopped"] is False
    assert stopped[0]["error_class"] == "external_conflict"
    remaining = report["remaining"]
    assert isinstance(remaining, list) and len(remaining) == 1
    assert report["paused"] is True


# -- select-runner -----------------------------------------------------------------------


def test_select_runner_records_the_qualified_image_by_digest_and_status_shows_it(
    ledger_service: LedgerService, ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, selected, _ = _run(
        capsys,
        "select-runner",
        "--database",
        str(ledger_path),
        "--image",
        _IMAGE,
        "--docker-context",
        "lima-enginery",
        admission=_fake_admission(),
    )
    _, again, _ = _run(
        capsys,
        "select-runner",
        "--database",
        str(ledger_path),
        "--image",
        _IMAGE,
        "--docker-context",
        "lima-enginery",
        admission=_fake_admission(),
    )

    assert code == 0
    assert selected["selected"] is True and selected["changed"] is True
    runner = selected["selected_runner"]
    assert isinstance(runner, dict)
    assert runner["image"] == _IMAGE
    assert runner["docker_context"] == "lima-enginery"
    assert runner["engine"]["server_version"] == "29.0.0"
    assert runner["image_identity"]["recipe_digest"] == "recipe-digest"
    assert again["changed"] is False
    _, status, _ = _run(capsys, "status", "--database", str(ledger_path))
    assert status["selected_runner"] == runner
    projection = ledger_service.read_projection(
        aggregate_type="operator_control", aggregate_id="control"
    )
    assert projection is not None and projection.aggregate_version == 1


def test_select_runner_records_nothing_when_admission_holds(
    ledger_service: LedgerService, ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from enginery.domain.errors import MissingPrerequisiteError

    class _Down:
        def qualify(self) -> EngineIdentity:
            raise MissingPrerequisiteError("docker is not running")

    admission = OciRunnerAdmission(engine_factory=lambda context: _Down())  # type: ignore[arg-type, return-value]

    code, report, _ = _run(
        capsys,
        "select-runner",
        "--database",
        str(ledger_path),
        "--image",
        _IMAGE,
        admission=admission,
    )

    assert code == 5
    assert report["selected"] is False and report["hold"] is not None
    _, status, _ = _run(capsys, "status", "--database", str(ledger_path))
    assert status["selected_runner"] is None


def test_select_runner_rejects_a_non_digest_image(
    ledger_service: LedgerService, ledger_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, error = _run(
        capsys,
        "select-runner",
        "--database",
        str(ledger_path),
        "--image",
        "latest",
        admission=_fake_admission(),
    )

    assert code == 2
    assert error["error"]["failure_class"] == "invalid_input"  # type: ignore[index]


# -- backup / restore --------------------------------------------------------------------


def _seed_control(ledger_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(capsys, "pause", "--database", str(ledger_path), "--reason", "seed")
    _run(capsys, "resume", "--database", str(ledger_path))


def test_backup_verifies_before_and_after_and_includes_artifacts(
    ledger_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "artifacts"
    service = LedgerService.open(ledger_path, artifact_store_root=artifacts)
    digest = service.publish_artifact_bytes(b"log", media_type="text/plain")
    service.close()
    _seed_control(ledger_path, capsys)
    output = tmp_path / "backup"

    code, report, _ = _run(
        capsys,
        "backup",
        "--database",
        str(ledger_path),
        "--output",
        str(output),
        "--artifacts",
        str(artifacts),
    )

    assert code == 0
    assert report["verified_before"]["healthy"] is True  # type: ignore[index]
    assert report["verified_after"]["healthy"] is True  # type: ignore[index]
    assert report["includes_artifacts"] is True
    assert ArtifactStore(output / "artifacts").verify(digest)


def test_backup_of_an_unhealthy_ledger_is_refused_before_anything_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    database.write_bytes(b"this is not a sqlite database at all" * 40)
    output = tmp_path / "backup"

    code, _, error = _run(capsys, "backup", "--database", str(database), "--output", str(output))

    assert code == 12
    assert error["error"]["failure_class"] == "validation_failure"  # type: ignore[index]
    assert not output.exists()


def _make_backup(ledger_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    _seed_control(ledger_path, capsys)
    output = tmp_path / "backup"
    code, _, _ = _run(capsys, "backup", "--database", str(ledger_path), "--output", str(output))
    assert code == 0
    return output


def test_restore_is_refused_unless_the_runtime_is_paused(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    before = ledger_path.read_bytes()

    code, _, error = _run(
        capsys, "restore", "--backup", str(backup), "--database", str(ledger_path)
    )

    assert code == 5
    assert "paused" in error["error"]["message"]  # type: ignore[index]
    assert ledger_path.read_bytes() == before
    assert not list(tmp_path.glob("ledger.db.pre-restore-*"))


def test_restore_is_refused_while_a_worker_runs_even_when_paused(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    worker_groups: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    _dispatch(ledger_service, tmp_path, worker_groups, seconds=60)
    _run(capsys, "pause", "--database", str(ledger_path))

    code, _, error = _run(
        capsys, "restore", "--backup", str(backup), "--database", str(ledger_path)
    )

    assert code == 5
    assert "still running" in error["error"]["message"]  # type: ignore[index]
    assert not list(tmp_path.glob("ledger.db.pre-restore-*"))


def test_restore_over_a_paused_ledger_keeps_the_prior_ledger_and_starts_paused(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    _run(
        capsys,
        "select-runner",
        "--database",
        str(ledger_path),
        "--image",
        _IMAGE,
        admission=_fake_admission(),
    )
    _run(capsys, "pause", "--database", str(ledger_path))
    ledger_service.close()

    code, report, _ = _run(
        capsys, "restore", "--backup", str(backup), "--database", str(ledger_path)
    )

    assert code == 0
    assert report["prior"] == "paused"
    assert report["verified_restored"]["healthy"] is True  # type: ignore[index]
    assert report["paused"] is True
    assert "restored from" in str(report["reason"])
    kept = Path(str(report["prior_ledger_kept_at"]))
    assert kept.exists() and kept.name.startswith("ledger.db.pre-restore-")
    # the restored ledger is the backed-up one (no runner selected then), now paused
    _, status, _ = _run(capsys, "status", "--database", str(ledger_path))
    assert status["selected_runner"] is None and status["paused"] is True
    # the prior ledger is intact and still has its selected runner
    _, prior_status, _ = _run(capsys, "status", "--database", str(kept))
    assert prior_status["selected_runner"] is not None


def test_restore_force_unpaused_overrides_the_pause_requirement(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    ledger_service.close()

    code, report, _ = _run(
        capsys,
        "restore",
        "--backup",
        str(backup),
        "--database",
        str(ledger_path),
        "--force-unpaused",
    )

    assert code == 0
    assert report["prior"] == "forced" and report["paused"] is True


def test_restore_into_a_fresh_location_needs_no_pause(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    target = tmp_path / "fresh" / "ledger.db"

    code, report, _ = _run(capsys, "restore", "--backup", str(backup), "--database", str(target))

    assert code == 0
    assert report["prior"] == "absent" and report["prior_ledger_kept_at"] is None
    assert target.is_file()


def test_restore_of_an_unreadable_ledger_requires_force(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    ledger_service.close()
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"garbage" * 500)

    refused, _, error = _run(capsys, "restore", "--backup", str(backup), "--database", str(corrupt))
    forced, report, _ = _run(
        capsys,
        "restore",
        "--backup",
        str(backup),
        "--database",
        str(corrupt),
        "--force-unpaused",
    )

    assert refused == 5 and "cannot prove" in error["error"]["message"]  # type: ignore[index]
    assert forced == 0
    assert str(report["prior"]).startswith("unreadable (forced)")
    assert Path(str(report["prior_ledger_kept_at"])).read_bytes() == b"garbage" * 500


def test_a_backup_that_fails_verification_aborts_before_the_database_is_touched(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    _run(capsys, "pause", "--database", str(ledger_path))
    ledger_service.close()
    (backup / "ledger.db").write_bytes(b"corrupted backup" * 300)
    before = ledger_path.read_bytes()

    code, _, error = _run(
        capsys, "restore", "--backup", str(backup), "--database", str(ledger_path)
    )

    assert code == 12
    assert error["error"]["failure_class"] == "validation_failure"  # type: ignore[index]
    assert ledger_path.read_bytes() == before
    assert not list(tmp_path.glob("ledger.db.pre-restore-*"))


def _break_manifest(backup: Path, defect: str) -> str:
    manifest = backup / "manifest.json"
    good = json.loads(manifest.read_text())
    if defect == "missing-manifest":
        manifest.unlink()
        return "no manifest.json"
    if defect == "malformed-json":
        manifest.write_text('{"created_at": "2026-', encoding="utf-8")
        return "not valid JSON"
    if defect == "not-an-object":
        manifest.write_text("[1, 2]", encoding="utf-8")
        return "must be a JSON object"
    if defect == "unreadable-bytes":
        manifest.write_bytes(b"\xff\xfe\x00garbage")
        return "cannot be read"
    if defect == "missing-field":
        del good["schema_version"]
        manifest.write_text(json.dumps(good), encoding="utf-8")
        return "'schema_version'"
    if defect == "wrong-type":
        good["includes_artifacts"] = "yes"
        manifest.write_text(json.dumps(good), encoding="utf-8")
        return "'includes_artifacts'"
    if defect == "bool-as-int":
        good["schema_version"] = True
        manifest.write_text(json.dumps(good), encoding="utf-8")
        return "'schema_version'"
    assert defect == "missing-database"
    (backup / "ledger.db").unlink()
    return "missing its ledger snapshot"


@pytest.mark.parametrize(
    "defect",
    [
        "missing-manifest",
        "malformed-json",
        "not-an-object",
        "unreadable-bytes",
        "missing-field",
        "wrong-type",
        "bool-as-int",
        "missing-database",
    ],
)
def test_restore_maps_a_defective_backup_manifest_to_invalid_input(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    defect: str,
) -> None:
    backup = _make_backup(ledger_path, tmp_path, capsys)
    _run(capsys, "pause", "--database", str(ledger_path))
    ledger_service.close()
    expected = _break_manifest(backup, defect)
    before = ledger_path.read_bytes()

    code, _, error = _run(
        capsys, "restore", "--backup", str(backup), "--database", str(ledger_path)
    )

    assert code == 2
    assert error["error"]["failure_class"] == "invalid_input"  # type: ignore[index]
    assert expected in error["error"]["message"]  # type: ignore[index]
    assert ledger_path.read_bytes() == before


def test_restore_of_a_missing_backup_directory_is_invalid_input(
    ledger_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, error = _run(
        capsys, "restore", "--backup", str(tmp_path / "nope"), "--database", str(ledger_path)
    )

    assert code == 2
    assert "is not a directory" in error["error"]["message"]  # type: ignore[index]


def test_a_restored_ledger_that_fails_verification_rolls_back_loudly(
    ledger_service: LedgerService,
    ledger_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import enginery.cli.operator as operator_cli
    from enginery.ledger.verify import VerificationIssue, VerificationReport

    backup = _make_backup(ledger_path, tmp_path, capsys)
    _run(capsys, "pause", "--database", str(ledger_path))
    ledger_service.close()
    real_verify = operator_cli.verify_ledger

    def verify(path: Path, *, artifact_store_root: Path | None = None) -> VerificationReport:
        if path == ledger_path:
            return VerificationReport(
                schema_version=1, issues=(VerificationIssue("integrity_check_failed", "bad"),)
            )
        return real_verify(path, artifact_store_root=artifact_store_root)

    monkeypatch.setattr(operator_cli, "verify_ledger", verify)

    code, _, error = _run(
        capsys, "restore", "--backup", str(backup), "--database", str(ledger_path)
    )

    assert code == 12
    assert "prior ledger was put back" in error["error"]["message"]  # type: ignore[index]
    monkeypatch.setattr(operator_cli, "verify_ledger", real_verify)
    _, status, _ = _run(capsys, "status", "--database", str(ledger_path))
    assert status["paused"] is True  # the prior (paused) ledger is back
    assert list(tmp_path.glob("ledger.db.failed-restore-*"))


# -- entry point -------------------------------------------------------------------------


def test_operator_entry_point_does_not_import_workflows() -> None:
    code = (
        "import sys, enginery.cli.operator; "
        "bad = sorted(m for m in sys.modules if m.startswith('enginery.workflows')); "
        "assert not bad, bad"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_operator_without_a_subcommand_is_invalid_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 2
    assert "enginery-operator" in capsys.readouterr().err
