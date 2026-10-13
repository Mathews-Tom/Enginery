"""Worker identity is bound to its host and boot; death and heartbeat loss are told apart.

Real worker subprocesses are started through the supervisor. A record from another host or
boot, or one persisted before host binding, is produced by rewriting the persisted state of
a genuinely launched worker, so every outcome is observed against a real live process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.domain.errors import HumanActionRequiredError
from enginery.engine.coordinator import Coordinator
from enginery.engine.leases import FencedNodeLease, FencedNodeLeases
from enginery.engine.recovery import (
    RecoveryCoordinator,
    WorkerLiveness,
    assess_orphan,
    assess_worker,
)
from enginery.engine.supervisor import (
    IDENTITY_VERSION,
    ProcessIdentity,
    WorkerSupervisor,
    identity_from_state,
    probe_host,
    probe_process,
)
from enginery.ledger.process_manager import ProcessManagerStateRecord
from enginery.ledger.service import LedgerService

_NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=60)
_KEY = "run-1:node-1"


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init",),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return repository


def _start(
    ledger: LedgerService,
    tmp_path: Path,
    *,
    command: tuple[str, ...] = (sys.executable, "-c", "import time; time.sleep(120)"),
) -> tuple[Coordinator, FencedNodeLease, ProcessIdentity, Path]:
    repository = _repository(tmp_path)
    coordinator = Coordinator(ledger, owner="operator")
    epoch = coordinator.acquire(now=_NOW, heartbeat_window=_WINDOW)
    lease = FencedNodeLeases(ledger, coordinator).grant(
        run_id="run-1",
        node_id="node-1",
        attempt_id="attempt-1",
        epoch=epoch.epoch,
        now=_NOW,
        lease_window=timedelta(minutes=10),
        expected_attempt_version=0,
        operation_id="operation-1",
    )
    identity = WorkerSupervisor(ledger, coordinator).start(
        lease=lease, command=command, cwd=repository, now=_NOW
    )
    return coordinator, lease, identity, repository


def _record(ledger: LedgerService) -> ProcessManagerStateRecord:
    record = ledger.read_process_manager_state(
        process_manager_name="worker-supervisor", state_key=_KEY
    )
    assert record is not None
    return record


def _rewrite(ledger: LedgerService, change: Callable[[dict[str, object]], None]) -> None:
    """Persist a different shape of the same worker record, as another version or host wrote it."""
    state = dict(_record(ledger).state)
    change(state)
    ledger.connection.execute(
        "UPDATE process_manager_state SET state_json = ? "
        "WHERE process_manager_name = 'worker-supervisor' AND state_key = ?",
        (json.dumps(state), _KEY),
    )
    ledger.connection.commit()


def _kill_and_reap(identity: ProcessIdentity) -> None:
    os.killpg(identity.process_group_id, signal.SIGKILL)
    os.waitpid(identity.pid, 0)


def _event_count(ledger: LedgerService, event_type: str) -> int:
    row = ledger.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND aggregate_id = ?",
        (event_type, _KEY),
    ).fetchone()
    return int(row[0])


def _terminate(identity: ProcessIdentity) -> None:
    if probe_process(identity.pid) is not None:
        _kill_and_reap(identity)


def test_a_new_worker_record_binds_the_host_and_boot(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, _, identity, _ = _start(ledger_service, tmp_path)
    try:
        state = _record(ledger_service).state
        host = probe_host()
        assert state["identity_version"] == IDENTITY_VERSION
        assert state["machine_id"] == host.machine_id
        assert state["boot_id"] == host.boot_id
        assert state["hostname"] == host.hostname
        decoded = identity_from_state(state)
        assert decoded == identity
        assert not decoded.legacy
        assert assess_worker(state).liveness is WorkerLiveness.ALIVE
    finally:
        _terminate(identity)


def test_a_record_persisted_before_host_binding_is_judged_by_single_host_semantics(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    coordinator, lease, identity, _ = _start(ledger_service, tmp_path)

    def strip_host(state: dict[str, object]) -> None:
        for key in ("identity_version", "machine_id", "boot_id", "hostname"):
            del state[key]

    _rewrite(ledger_service, strip_host)
    # A genuine legacy record carries the start time as the inherited environment formatted it.
    inherited = probe_process(identity.pid, inherited_environment=True)
    assert inherited is not None
    _rewrite(ledger_service, lambda state: state.update(start_identity=inherited.start_identity))
    legacy = identity_from_state(_record(ledger_service).state)
    assert legacy.legacy
    assert (legacy.pid, legacy.process_group_id, legacy.start_identity) == (
        identity.pid,
        identity.process_group_id,
        inherited.start_identity,
    )
    assert assess_worker(_record(ledger_service).state).liveness is WorkerLiveness.ALIVE

    # It still supports the full lifecycle: the live worker is cancelled and the exit is
    # recorded without inventing a host for the old record.
    WorkerSupervisor(ledger_service, coordinator).cancel_persisted(lease=lease, now=_NOW)

    after = _record(ledger_service).state
    assert after["status"] == "exit_observed"
    assert "identity_version" not in after and "hostname" not in after
    assert assess_worker(after).liveness is WorkerLiveness.EXITED
    assert probe_process(identity.pid) is None


@pytest.mark.parametrize("difference", ["machine"])
def test_a_record_from_another_host_or_boot_is_held_and_its_pid_is_never_signalled(
    ledger_service: LedgerService, tmp_path: Path, difference: str
) -> None:
    coordinator, lease, identity, repository = _start(ledger_service, tmp_path)
    try:

        def foreign(state: dict[str, object]) -> None:
            state["machine_id" if difference == "machine" else "boot_id"] = f"another-{difference}"

        _rewrite(ledger_service, foreign)
        recorded = _record(ledger_service)
        assert assess_worker(recorded.state).liveness is WorkerLiveness.FOREIGN_HOST
        orphan = assess_orphan(process_state=recorded, workspace_path=repository)
        assert (orphan.ready_to_release, orphan.reason) == (False, "worker_host_identity_differs")
        reconciled = RecoveryCoordinator(ledger_service, coordinator).reconcile(
            lease=lease, workspace_path=repository, epoch=lease.epoch, now=_NOW
        )
        assert (reconciled.ready_to_release, reconciled.reason) == (
            False,
            "worker_host_identity_differs",
        )

        supervisor = WorkerSupervisor(ledger_service, coordinator)
        foreign_identity = identity_from_state(recorded.state)
        with pytest.raises(HumanActionRequiredError, match="different host or boot"):
            supervisor.cancel_persisted(lease=lease, now=_NOW)
        with pytest.raises(HumanActionRequiredError, match="different host or boot"):
            supervisor.enforce_heartbeat(
                lease=lease, identity=foreign_identity, now=_NOW + timedelta(minutes=5)
            )
        with pytest.raises(HumanActionRequiredError, match="different host or boot"):
            supervisor.observe_exit(lease=lease, coordinator_epoch=lease.epoch, now=_NOW)

        # The pid names an unrelated process here: nothing was signalled, recorded or reaped.
        assert probe_process(identity.pid) == identity
        assert _record(ledger_service).state["status"] == "running"
        assert _event_count(ledger_service, "worker.termination_requested") == 0
    finally:
        _terminate(identity)


def test_an_unsupported_identity_version_is_unverifiable_and_blocks_recovery(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    _, _, identity, repository = _start(ledger_service, tmp_path)
    try:

        def future(state: dict[str, object]) -> None:
            state["identity_version"] = IDENTITY_VERSION + 1

        _rewrite(ledger_service, future)
        recorded = _record(ledger_service)

        assert assess_worker(recorded.state).liveness is WorkerLiveness.UNVERIFIABLE
        orphan = assess_orphan(process_state=recorded, workspace_path=repository)
        assert (orphan.ready_to_release, orphan.reason) == (
            False,
            "supervisor_identity_missing_or_invalid",
        )
    finally:
        _terminate(identity)


def test_worker_death_pid_reuse_and_heartbeat_loss_are_classified_apart(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    coordinator, lease, identity, repository = _start(ledger_service, tmp_path)
    supervisor = WorkerSupervisor(ledger_service, coordinator)

    # Heartbeat loss: the coordinator epoch lapsed but the exact worker is still running.
    later = _NOW + timedelta(minutes=5)
    assert coordinator.current_epoch() is not None
    assert not coordinator.current_epoch().active_at(later)  # type: ignore[union-attr]
    assert assess_worker(_record(ledger_service).state).liveness is WorkerLiveness.ALIVE
    held = assess_orphan(process_state=_record(ledger_service), workspace_path=repository)
    assert (held.ready_to_release, held.reason) == (False, "process_still_running")

    # PID reuse: the pid is held by a process with a different start identity.
    original = dict(_record(ledger_service).state)
    _rewrite(ledger_service, lambda state: state.update(start_identity="a different start"))
    reused = _record(ledger_service)
    assert assess_worker(reused.state).liveness is WorkerLiveness.PID_REUSED
    refusal = assess_orphan(process_state=reused, workspace_path=repository)
    assert (refusal.ready_to_release, refusal.reason) == (
        True,
        "process_absent_workspace_quiescent",
    )
    # The recorded worker is provably gone, so cancel reaches a bounded state without
    # signalling the unrelated process that now holds the pid.
    supervisor.cancel_persisted(lease=lease, now=_NOW)
    assert probe_process(identity.pid) == identity
    assert _record(ledger_service).state["status"] == "exit_observed"
    assert _record(ledger_service).state["exit_reason"] == "start_identity_mismatch"
    assert _event_count(ledger_service, "worker.cancelled") == 1
    _rewrite(ledger_service, lambda state: state.update(start_identity=original["start_identity"]))

    # Death: the worker is killed outright; no coordinator expiry is involved.
    _kill_and_reap(identity)
    assert assess_worker(_record(ledger_service).state).liveness is WorkerLiveness.EXITED


def test_enforced_heartbeat_loss_terminates_the_exact_live_worker(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    coordinator, lease, identity, _ = _start(ledger_service, tmp_path)

    assert WorkerSupervisor(ledger_service, coordinator).enforce_heartbeat(
        lease=lease, identity=identity, now=_NOW + timedelta(minutes=5)
    )

    assert probe_process(identity.pid) is None
    assert assess_worker(_record(ledger_service).state).liveness is WorkerLiveness.EXITED


def test_reconcile_holds_a_worker_record_that_belongs_to_a_different_attempt(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    coordinator, lease, identity, repository = _start(ledger_service, tmp_path)
    _kill_and_reap(identity)
    _rewrite(ledger_service, lambda state: state.update(attempt_id="attempt-other"))

    assessment = RecoveryCoordinator(ledger_service, coordinator).reconcile(
        lease=lease, workspace_path=repository, epoch=lease.epoch, now=_NOW
    )

    assert (assessment.ready_to_release, assessment.reason) == (
        False,
        "worker_attempt_identity_mismatch",
    )


def test_terminating_a_foreign_identity_directly_refuses_and_leaves_the_process_alone(
    tmp_path: Path,
) -> None:
    from enginery.engine.supervisor import HostIdentity, _terminate_exact_group

    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        local = probe_process(bystander.pid)
        assert local is not None
        foreign = ProcessIdentity(
            local.pid,
            local.process_group_id,
            local.start_identity,
            HostIdentity("another-host", "another-boot"),
        )

        with pytest.raises(HumanActionRequiredError, match="different host or boot"):
            _terminate_exact_group(foreign)

        assert bystander.poll() is None
        assert probe_process(bystander.pid) == local
    finally:
        bystander.kill()
        bystander.wait()


_PROBE = (
    "import json; from enginery.engine.supervisor import probe_host; h = probe_host(); "
    "print(json.dumps([h.machine_id, h.boot_id]))"
)


def _identity_under_timezone(timezone: str) -> list[str]:
    env = {**os.environ, "TZ": timezone}
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, text=True, capture_output=True, check=True
    )
    return list(json.loads(completed.stdout))


def test_host_and_boot_identity_is_stable_across_processes_and_time_zones() -> None:
    first = _identity_under_timezone("UTC")
    second = _identity_under_timezone("Pacific/Kiritimati")
    third = _identity_under_timezone("America/Los_Angeles")

    assert first == second == third
    assert all(value and "UTC" not in value for value in first)
    local = probe_host()
    assert first == [local.machine_id, local.boot_id]


def test_a_hostname_change_does_not_change_whether_a_worker_is_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.engine.supervisor import HostIdentity, is_local

    recorded = probe_process(os.getpid())
    assert recorded is not None and recorded.host is not None
    renamed = ProcessIdentity(
        recorded.pid,
        recorded.process_group_id,
        recorded.start_identity,
        HostIdentity(recorded.host.machine_id, recorded.host.boot_id, "renamed-laptop"),
    )
    monkeypatch.setattr("socket.gethostname", lambda: "a-third-name")
    probe_host.cache_clear()
    try:
        assert probe_host().hostname == "a-third-name"
        assert probe_host() == recorded.host
        assert is_local(renamed)
        assert renamed == recorded
    finally:
        monkeypatch.undo()
        probe_host.cache_clear()


def test_an_unreadable_host_id_fails_closed_instead_of_being_guessed(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enginery.domain.errors import InternalInvariantViolationError

    coordinator, lease, identity, repository = _start(ledger_service, tmp_path)
    try:
        state = dict(_record(ledger_service).state)
        monkeypatch.setattr("enginery.engine.supervisor._command_output", lambda _command: "")
        monkeypatch.setattr("enginery.engine.supervisor._first_file_text", lambda *_paths: "")
        probe_host.cache_clear()
        with pytest.raises(InternalInvariantViolationError, match="refusing to guess"):
            probe_host()
        assert assess_worker(state).liveness is WorkerLiveness.UNVERIFIABLE
        with pytest.raises(InternalInvariantViolationError, match="refusing to guess"):
            WorkerSupervisor(ledger_service, coordinator).start(
                lease=lease,
                command=(sys.executable, "-c", "pass"),
                cwd=repository,
                now=_NOW,
            )
    finally:
        monkeypatch.undo()
        probe_host.cache_clear()
        _terminate(identity)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal pid 1")
def test_a_pid_held_by_another_users_process_is_pid_reused_and_never_crashes() -> None:
    host = probe_host()
    state: dict[str, object] = {
        "pid": 1,
        "process_group_id": 1,
        "start_identity": "recorded long ago",
        "identity_version": IDENTITY_VERSION,
        "machine_id": host.machine_id,
        "boot_id": host.boot_id,
        "hostname": host.hostname,
    }

    assert assess_worker(state).liveness is WorkerLiveness.PID_REUSED


def test_a_legacy_record_with_a_mismatched_start_time_is_unverifiable_never_gone(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    coordinator, lease, identity, repository = _start(ledger_service, tmp_path)
    try:

        def legacy(state: dict[str, object]) -> None:
            for key in ("identity_version", "machine_id", "boot_id", "hostname"):
                del state[key]
            state["start_identity"] = "Thu Jan  1 00:00:00 1970"

        _rewrite(ledger_service, legacy)
        recorded = _record(ledger_service)
        assert identity_from_state(recorded.state).legacy

        assert assess_worker(recorded.state).liveness is WorkerLiveness.UNVERIFIABLE
        orphan = assess_orphan(process_state=recorded, workspace_path=repository)
        assert (orphan.ready_to_release, orphan.reason) == (
            False,
            "supervisor_identity_missing_or_invalid",
        )
        supervisor = WorkerSupervisor(ledger_service, coordinator)
        with pytest.raises(HumanActionRequiredError, match="cannot be verified"):
            supervisor.cancel_persisted(lease=lease, now=_NOW)
        with pytest.raises(HumanActionRequiredError, match="cannot be verified"):
            supervisor.observe_exit(lease=lease, coordinator_epoch=lease.epoch, now=_NOW)
        with pytest.raises(HumanActionRequiredError, match="cannot be verified"):
            supervisor.enforce_heartbeat(
                lease=lease,
                identity=identity_from_state(recorded.state),
                now=_NOW + timedelta(minutes=5),
            )
        assert probe_process(identity.pid) == identity
        assert _record(ledger_service).state["status"] == "running"
        assert _event_count(ledger_service, "worker.termination_requested") == 0
    finally:
        _terminate(identity)


@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal pid 1")
def test_a_legacy_record_on_a_pid_this_user_cannot_signal_is_unverifiable() -> None:
    state: dict[str, object] = {
        "pid": 1,
        "process_group_id": 1,
        "start_identity": "recorded long ago",
    }

    assert assess_worker(state).liveness is WorkerLiveness.UNVERIFIABLE


def test_terminating_a_legacy_identity_with_a_mismatched_start_time_refuses_directly() -> None:
    from enginery.engine.supervisor import _terminate_exact_group

    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        local = probe_process(bystander.pid)
        assert local is not None
        legacy = ProcessIdentity(
            local.pid, local.process_group_id, "Thu Jan  1 00:00:00 1970", None
        )
        assert legacy.legacy

        with pytest.raises(HumanActionRequiredError, match="cannot be verified"):
            _terminate_exact_group(legacy)

        assert bystander.poll() is None
        assert probe_process(bystander.pid) == local
    finally:
        bystander.kill()
        bystander.wait()
