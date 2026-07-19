from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessTask
from enginery.domain.artifact import RedactionClassification
from enginery.domain.errors import WorkerFailureError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.ledger.artifact_store import ArtifactStore


class RecordedProcess:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.pid = 4242
        self.running = True

    def communicate(self) -> tuple[str, None]:
        self.running = False
        return self.output, None

    def poll(self) -> int | None:
        return None if self.running else self.returncode


def _task(workspace: Path) -> HarnessTask:
    return HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId("omp-start-1"),
        workspace_path=workspace,
        objective="Implement the requested provider behavior.",
        acceptance_criteria=("tests pass",),
        constraints=("no credentials in output",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=Decimal("1.00"),
    )


def _events(*, include_terminal: bool = True, secret: bool = False) -> str:
    records: list[dict[str, object]] = [
        {"type": "session", "version": 3},
        {"type": "agent_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}},
    ]
    if secret:
        records.append(
            {"type": "message_update", "token": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"}
        )
    if include_terminal:
        records.append({"type": "agent_end"})
    return "\n".join(json.dumps(record) for record in records)


def test_omp_harness_normalizes_events_and_redacts_artifact_output(tmp_path: Path) -> None:
    process = RecordedProcess(_events(secret=True))
    commands: list[tuple[str, ...]] = []

    def start(arguments: tuple[str, ...], workspace: Path) -> RecordedProcess:
        commands.append(arguments)
        assert workspace == tmp_path
        return process

    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=start,
    )
    session = harness.start(_task(tmp_path))

    events = tuple(harness.events(session))
    result = harness.result(session)

    assert [event.kind.value for event in events] == [
        "progress",
        "started",
        "progress",
        "progress",
        "terminal",
    ]
    assert result.terminal_status == "succeeded"
    assert result.outputs[0].redaction is RedactionClassification.SENSITIVE
    assert b"ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in harness.artifact_store.read_bytes(
        result.outputs[0].digest
    )
    assert "--mode=json" in commands[0]
    assert "--no-session" in commands[0]
    assert "--api-key" not in commands[0]


def test_omp_harness_builds_a_supervised_worker_command(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )

    command = harness.supervised_command(
        _task(tmp_path),
        result_path=tmp_path / "worker-result.json",
    )

    assert command[:7] == (
        sys.executable,
        "-m",
        "enginery.engine.omp_worker",
        "--operation-id",
        "omp-start-1",
        "--output",
        str(tmp_path / "worker-result.json"),
    )
    assert command[7] == "--"
    assert command[8] == "omp"


def test_omp_harness_rejects_malformed_or_incomplete_events(tmp_path: Path) -> None:
    malformed = RecordedProcess("not-json")

    def start(arguments: tuple[str, ...], workspace: Path) -> RecordedProcess:
        return malformed

    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=start,
    )
    session = harness.start(_task(tmp_path))

    with pytest.raises(WorkerFailureError, match="malformed"):
        tuple(harness.events(session))


def test_omp_harness_cancels_the_process_group(tmp_path: Path) -> None:
    process = RecordedProcess(_events(), returncode=-15)
    terminated: list[int] = []

    def start(arguments: tuple[str, ...], workspace: Path) -> RecordedProcess:
        return process

    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=start,
        terminator=terminated.append,
    )
    session = harness.start(_task(tmp_path))

    harness.cancel(session, operation_id=OperationId("omp-cancel-1"))
    result = harness.result(session)

    assert terminated == [4242]
    assert result.terminal_status == "cancelled"
