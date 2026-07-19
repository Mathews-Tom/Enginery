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
from enginery.engine.omp_worker import run_omp_worker
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
        {"type": "tool_execution_start", "toolName": "git"},
        {"type": "tool_execution_update", "toolName": "git"},
        {"type": "tool_execution_end", "toolName": "git"},
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
        "progress",
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


def test_omp_harness_collects_a_supervised_worker_result(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    task = _task(tmp_path)
    result_path = tmp_path / "worker-result.json"
    exit_code = run_omp_worker(
        operation_id=str(task.operation_id),
        command=(sys.executable, "-c", f"print({json.dumps(_events())})"),
        output_path=result_path,
    )

    events, result = harness.collect_supervised(task, result_path=result_path)

    assert exit_code == 0
    assert events[-1].kind.value == "terminal"
    assert result.terminal_status == "succeeded"
    assert harness.artifact_store.read_bytes(result.outputs[0].digest) == _events().encode() + b"\n"


def test_omp_harness_rejects_supervised_result_for_another_operation(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    result_path = tmp_path / "worker-result.json"
    result_path.write_text(
        json.dumps(
            {
                "operation_id": "other-operation",
                "output": _events(),
                "schema_version": 1,
                "terminal_status": "succeeded",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkerFailureError, match="operation"):
        harness.collect_supervised(_task(tmp_path), result_path=result_path)


def test_omp_harness_rejects_unsupported_supervised_result_schema(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    result_path = tmp_path / "worker-result.json"
    result_path.write_text(
        json.dumps(
            {
                "operation_id": "omp-operation-1",
                "output": _events(),
                "schema_version": 2,
                "terminal_status": "succeeded",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkerFailureError, match="schema version"):
        harness.collect_supervised(_task(tmp_path), result_path=result_path)


@pytest.mark.parametrize(
    ("output", "match"),
    (
        ("not-json", "malformed"),
        (json.dumps({"type": "future_event"}) + "\n" + _events(), "unsupported"),
        (_events(include_terminal=False), "agent_end"),
    ),
)
def test_omp_harness_rejects_malformed_unknown_or_incomplete_events(
    tmp_path: Path, output: str, match: str
) -> None:
    process = RecordedProcess(output)

    def start(arguments: tuple[str, ...], workspace: Path) -> RecordedProcess:
        return process

    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=start,
    )
    session = harness.start(_task(tmp_path))

    with pytest.raises(WorkerFailureError, match=match):
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
