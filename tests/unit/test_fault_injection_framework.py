from __future__ import annotations

import pytest

from fault_injection.framework import FaultScenario, main_for, print_report, run_scenarios


def _passing() -> None:
    assert 1 + 1 == 2


def _failing() -> None:
    raise AssertionError("deliberately broken")


def test_run_scenarios_reports_pass_and_fail_independently() -> None:
    scenarios = (
        FaultScenario(name="passing", description="always passes", run=_passing),
        FaultScenario(name="failing", description="always fails", run=_failing),
    )

    report = run_scenarios(scenarios)

    assert report.ok is False
    results_by_name = {result.name: result for result in report.results}
    assert results_by_name["passing"].passed is True
    assert results_by_name["failing"].passed is False
    assert "deliberately broken" in results_by_name["failing"].detail


def test_run_scenarios_all_passing_reports_ok() -> None:
    scenarios = (FaultScenario(name="passing", description="always passes", run=_passing),)

    report = run_scenarios(scenarios)

    assert report.ok is True


def test_one_failing_scenario_does_not_prevent_later_scenarios_from_running() -> None:
    calls: list[str] = []

    def first() -> None:
        calls.append("first")
        raise AssertionError("boom")

    def second() -> None:
        calls.append("second")

    scenarios = (
        FaultScenario(name="first", description="fails", run=first),
        FaultScenario(name="second", description="passes", run=second),
    )

    run_scenarios(scenarios)

    assert calls == ["first", "second"]


def test_main_for_returns_zero_iff_every_scenario_passed() -> None:
    all_passing = (FaultScenario(name="a", description="", run=_passing),)
    assert main_for(all_passing) == 0

    one_failing = (
        FaultScenario(name="a", description="", run=_passing),
        FaultScenario(name="b", description="", run=_failing),
    )
    assert main_for(one_failing) == 1


def test_print_report_writes_pass_and_fail_lines(capsys: pytest.CaptureFixture[str]) -> None:
    scenarios = (
        FaultScenario(name="passing", description="always passes", run=_passing),
        FaultScenario(name="failing", description="always fails", run=_failing),
    )
    report = run_scenarios(scenarios)

    print_report(report, scenarios=scenarios)

    captured = capsys.readouterr()
    assert "[PASS] passing: always passes" in captured.out
    assert "[FAIL] failing: always fails" in captured.out
    assert "1/2 scenarios passed" in captured.out
