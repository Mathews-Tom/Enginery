"""omp usage reports read from direct and supervised collection, without inflation or coercion."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.execution import selection_for
from tests.support.omp_usage import UNREPORTED, message_end_lines, omp_transcript, omp_usage, render

from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessResult, HarnessTask
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.reported_usage import ReportedUsage, UsageReportState, UsageTokens
from enginery.engine.omp_worker import run_omp_worker
from enginery.ledger.artifact_store import ArtifactStore

_FIRST = omp_usage(
    input_tokens=1200,
    output_tokens=300,
    cache_read=5000,
    cache_write=100,
    cost=(0.0012, 0.003, 0.0005, 0.000125),
)
_SECOND = omp_usage(
    input_tokens=40, output_tokens=7, cache_read=6200, cost=(0.00004, 0.00007, 0.00062, 0.0)
)


class _Process:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.output = output
        self.returncode: int | None = returncode
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
        operation_id=OperationId("omp-usage-1"),
        workspace_path=workspace,
        objective="Make one bounded change.",
        acceptance_criteria=("tests pass",),
        constraints=("retain evidence",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=Decimal("1.00"),
        selection=selection_for("omp"),
    )


def _direct(tmp_path: Path, process: _Process) -> tuple[OmpHarness, HarnessResult]:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=lambda arguments, workspace: process,
    )
    session = harness.start(_task(tmp_path))
    return harness, harness.result(session)


def _supervised(tmp_path: Path, script: str) -> tuple[OmpHarness, HarnessResult, Path]:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    task = _task(tmp_path)
    result_path = tmp_path / "worker-result.json"
    run_omp_worker(
        operation_id=str(task.operation_id),
        command=(sys.executable, "-c", script),
        output_path=result_path,
    )
    _, result = harness.collect_supervised(task, result_path=result_path)
    return harness, result, result_path


def _usage(result: HarnessResult) -> ReportedUsage:
    assert result.reported_usage is not None
    return result.reported_usage


def _tokens(result: HarnessResult) -> UsageTokens:
    tokens = _usage(result).tokens
    assert tokens is not None
    return tokens


def _artifact_text(harness: OmpHarness, result: HarnessResult) -> str:
    return harness.artifact_store.read_bytes(_usage(result).source_artifact).decode()


def test_direct_collection_counts_each_finalized_assistant_message_once(tmp_path: Path) -> None:
    harness, result = _direct(tmp_path, _Process(render(omp_transcript(_FIRST, _SECOND))))

    usage = _usage(result)
    text = _artifact_text(harness, result)
    assert usage.source_artifact == result.outputs[0].digest
    assert usage.reported_by == "harness"
    assert usage.state is UsageReportState.REPORTED
    # Streaming snapshots, tool results and turn_end/agent_end copies are not reports.
    assert usage.reported_lines == tuple(message_end_lines(text)) and len(usage.reported_lines) == 2
    assert usage.tokens is not None and usage.cost is not None
    assert usage.tokens.to_state() == {
        "input": 1240,
        "output": 307,
        "cache_read": 11200,
        "cache_write": 100,
        "total": 1547 + 11300,
    }
    reported_totals = (_FIRST["cost"], _SECOND["cost"])
    assert Decimal(repr(usage.cost.total)) == sum(
        (Decimal(repr(cost["total"])) for cost in reported_totals),  # type: ignore[index]
        Decimal(0),
    )
    assert usage.cost.cache_write == 0.000125


def test_repeated_direct_collection_returns_the_same_report(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=lambda arguments, workspace: _Process(render(omp_transcript(_FIRST))),
    )
    session = harness.start(_task(tmp_path))

    first = harness.result(session)
    tuple(harness.events(session))

    assert harness.result(session) is first
    assert _tokens(first).input == 1200


def test_repeated_supervised_collection_reports_identical_usage(tmp_path: Path) -> None:
    text = render(omp_transcript(_FIRST))
    harness, first, result_path = _supervised(tmp_path, f"print({text!r})")

    _, again = harness.collect_supervised(_task(tmp_path), result_path=result_path)

    assert again.reported_usage == first.reported_usage
    assert _tokens(again).input == 1200


def test_a_failed_supervised_attempt_keeps_its_finalized_usage_and_failed_status(
    tmp_path: Path,
) -> None:
    text = render(omp_transcript(_FIRST, terminal=False))
    script = f"import sys\nprint({text!r})\nprint('provider stream reset', file=sys.stderr)\n"
    script += "raise SystemExit(2)\n"

    harness, result, _ = _supervised(tmp_path, script)

    usage = _usage(result)
    assert result.terminal_status == "failed"
    assert usage.state is UsageReportState.REPORTED
    assert usage.reported_lines == tuple(message_end_lines(_artifact_text(harness, result)))
    assert usage.unparsed_lines == 1  # the diagnostic line, counted rather than read
    assert usage.tokens is not None and usage.tokens.output == 300


def test_a_cancelled_direct_attempt_keeps_usage_finalized_before_a_truncated_line(
    tmp_path: Path,
) -> None:
    output = render(omp_transcript(_FIRST, terminal=False)) + '\n{"type": "message_end", "mess'
    process = _Process(output, returncode=-15)
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        process_factory=lambda arguments, workspace: process,
        terminator=lambda pid: None,
    )
    session = harness.start(_task(tmp_path))
    harness.cancel(session, operation_id=OperationId("omp-usage-1"))

    result = harness.result(session)

    assert result.terminal_status == "cancelled"
    assert _usage(result).unparsed_lines == 1
    assert _tokens(result).input == 1200


def test_explicit_zero_usage_is_a_report_not_an_absence(tmp_path: Path) -> None:
    zero = omp_usage(input_tokens=0, output_tokens=0)
    _, result = _direct(tmp_path, _Process(render(omp_transcript(zero))))

    usage = _usage(result)
    assert usage.state is UsageReportState.REPORTED
    assert usage.tokens is not None and set(usage.tokens.to_state().values()) == {0}
    assert usage.cost is not None and set(usage.cost.to_state().values()) == {0.0}


def test_no_finalized_assistant_message_leaves_usage_unreported(tmp_path: Path) -> None:
    _, result = _direct(tmp_path, _Process(render(omp_transcript())))

    assert result.reported_usage is None


def test_a_message_without_usage_makes_the_report_partial(tmp_path: Path) -> None:
    harness, result = _direct(tmp_path, _Process(render(omp_transcript(_FIRST, UNREPORTED))))

    usage = _usage(result)
    lines = message_end_lines(_artifact_text(harness, result))
    assert usage.state is UsageReportState.PARTIAL
    assert (usage.reported_lines, usage.unreported_lines) == ((lines[0],), (lines[1],))
    assert usage.tokens is not None and usage.tokens.input == 1200


@pytest.mark.parametrize(
    ("usage", "reason"),
    (
        ({**_FIRST, "input": -1}, "tokens.input"),
        ({**_FIRST, "output": True}, "tokens.output"),
        ({**_FIRST, "cacheRead": "5000"}, "tokens.cache_read"),
        ({**_FIRST, "totalTokens": 1.5}, "tokens.total"),
        ({key: value for key, value in _FIRST.items() if key != "cacheWrite"}, "usage.cacheWrite"),
        ({**_FIRST, "cost": None}, "usage.cost"),
        ({**_FIRST, "cost": {**_FIRST["cost"], "total": -0.1}}, "cost.total"),  # type: ignore[dict-item]
        ([], "usage must be an object"),
        # Beyond what a JSON harness can report exactly; also keeps every sum finite.
        ({**_FIRST, "input": 2**53}, "tokens.input exceeds"),
        ({**_FIRST, "cost": {**_FIRST["cost"], "input": 10**400}}, "cost.input exceeds"),  # type: ignore[dict-item]
        ({**_FIRST, "cost": {**_FIRST["cost"], "total": 1.7e308}}, "cost.total exceeds"),  # type: ignore[dict-item]
    ),
)
def test_a_malformed_present_report_is_retained_visibly_not_coerced(
    tmp_path: Path, usage: object, reason: str
) -> None:
    harness, result = _direct(tmp_path, _Process(render(omp_transcript(_SECOND, usage))))

    report = _usage(result)
    assert result.terminal_status == "succeeded"  # usage is evidence, not the worker's verdict
    assert report.state is UsageReportState.MALFORMED
    assert report.tokens is None and report.cost is None
    assert [entry.line for entry in report.malformed] == message_end_lines(
        _artifact_text(harness, result)
    )[1:]
    assert reason in report.malformed[0].reason
    assert ReportedUsage.from_state(json.loads(json.dumps(report.to_state()))) == report


def test_unparseable_worker_lines_cannot_fail_a_failed_attempts_collection(
    tmp_path: Path,
) -> None:
    text = render(omp_transcript(_FIRST, terminal=False))
    script = (
        f"print({text!r})\n"
        "print('9' * 4301)\n"  # beyond Python's integer-parsing limit
        "print('[' * 100000)\n"  # nesting beyond the parser's recursion limit
        "raise SystemExit(3)\n"
    )

    harness, result, _ = _supervised(tmp_path, script)

    assert result.terminal_status == "failed"
    assert _usage(result).unparsed_lines == 2
    assert _usage(result).reported_lines == tuple(
        message_end_lines(_artifact_text(harness, result))
    )
    assert _tokens(result).input == 1200
