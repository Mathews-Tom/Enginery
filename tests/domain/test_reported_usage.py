"""Persisted reported-usage records decode exactly or fail; nothing is coerced to zero."""

from __future__ import annotations

import json
from typing import Any

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.reported_usage import (
    MAX_RECORDED_LINES,
    ReportedUsage,
    UsageCost,
    UsageReportBuilder,
    UsageReportState,
    UsageTokens,
)

_ARTIFACT = Digest.of_bytes(b"omp output")


def _report(*, unreported: tuple[int, ...] = (), malformed: tuple[int, ...] = ()) -> ReportedUsage:
    builder = UsageReportBuilder()
    for line, cost in ((3, 0.1), (9, 0.2)):
        builder.add_report(
            line, UsageTokens(10, 10, 0, 0, 20), UsageCost(cost, cost, 0.0, 0.0, 2 * cost)
        )
    for line in unreported:
        builder.add_unreported(line)
    for line in malformed:
        builder.add_malformed(line, "usage.cost must be an object")
    report = builder.build(source_artifact=_ARTIFACT, reported_by="harness")
    assert report is not None
    return report


def _persisted(report: ReportedUsage) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(json.dumps(report.to_state()))
    return state


@pytest.mark.parametrize(
    "report",
    (_report(), _report(unreported=(16,)), _report(malformed=(12,))),
    ids=("reported", "partial", "malformed"),
)
def test_a_persisted_report_round_trips_exactly(report: ReportedUsage) -> None:
    assert ReportedUsage.from_state(_persisted(report)) == report


def test_reported_cost_sums_decimally_and_tokens_exactly() -> None:
    report = _report()

    assert report.cost == UsageCost(0.3, 0.3, 0.0, 0.0, 0.6)
    assert report.tokens == UsageTokens(20, 20, 0, 0, 40)


def test_report_states_distinguish_partial_and_malformed_from_complete() -> None:
    assert _report().state is UsageReportState.REPORTED
    partial = _report(unreported=(16,))
    assert partial.state is UsageReportState.PARTIAL
    assert partial.tokens == UsageTokens(20, 20, 0, 0, 40)  # totals cover reported messages
    malformed = _report(malformed=(12,))
    assert malformed.state is UsageReportState.MALFORMED
    assert (malformed.tokens, malformed.cost) == (None, None)


def test_no_present_report_is_unmeasured_not_zero() -> None:
    builder = UsageReportBuilder()
    builder.add_unreported(4)
    builder.add_unparsed()

    assert builder.build(source_artifact=_ARTIFACT, reported_by="harness") is None


def test_counts_stay_complete_while_kept_lines_are_bounded() -> None:
    builder = UsageReportBuilder()
    messages = MAX_RECORDED_LINES + 5
    for line in range(1, messages + 1):
        builder.add_report(line, UsageTokens(1, 0, 0, 0, 1), UsageCost(0.0, 0.0, 0.0, 0.0, 0.0))
        builder.add_malformed(messages + line, "usage must be an object")

    report = builder.build(source_artifact=_ARTIFACT, reported_by="worker")

    assert report is not None
    assert (report.reported_messages, report.malformed_messages) == (messages, messages)
    assert report.reported_lines == tuple(range(1, MAX_RECORDED_LINES + 1))
    assert len(report.malformed) == MAX_RECORDED_LINES
    assert ReportedUsage.from_state(_persisted(report)) == report


@pytest.mark.parametrize(
    "damage",
    (
        lambda state: state.update(state="reported", unreported_lines=[16]),
        lambda state: state.update(extra=True),
        lambda state: state.update(schema_version=True),
        lambda state: state.update(reported_by="provider"),
        lambda state: state["messages"].update(reported=3),
        lambda state: state["messages"].update(reported=-1),
        lambda state: state["tokens"].update(input=True),
        lambda state: state["tokens"].update(output=-1),
        lambda state: state["tokens"].update(total=1.0),
        lambda state: state["tokens"].pop("cache_read"),
        lambda state: state["cost"].update(total=float("inf")),
        lambda state: state["cost"].update(total=10**400),
        lambda state: state["cost"].update(input="0.1"),
        lambda state: state.update(tokens=None),
        lambda state: state.update(reported_lines=[9, 3]),
        lambda state: state["source"].update(artifact_digest="sha256:short"),
        lambda state: state["source"].update(record="turn_end"),
    ),
)
def test_a_damaged_persisted_report_is_rejected_not_repaired(damage: Any) -> None:
    state = _persisted(_report())
    damage(state)

    with pytest.raises(InvalidInputError):
        ReportedUsage.from_state(state)
