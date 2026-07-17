"""A minimal, dependency-free fault-scenario runner.

A :class:`FaultScenario` is a name, a description, and a zero-argument
callable that raises on failure — an ``AssertionError`` for a violated
expectation, or any other exception surfaced by the code under test.
:func:`run_scenarios` never lets one scenario's exception abort the run;
it records pass/fail per scenario so a single regression is reported
precisely instead of stopping the whole suite. :func:`main_for` gives
every fault-injection script the same process-exit-code contract: ``0``
iff every scenario passed.
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FaultScenario:
    name: str
    description: str
    run: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class FaultReport:
    results: tuple[ScenarioResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.passed for result in self.results)


def run_scenarios(scenarios: Sequence[FaultScenario]) -> FaultReport:
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            scenario.run()
        except Exception as error:
            detail = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
            results.append(ScenarioResult(name=scenario.name, passed=False, detail=detail))
        else:
            results.append(ScenarioResult(name=scenario.name, passed=True, detail="ok"))
    return FaultReport(results=tuple(results))


def print_report(report: FaultReport, *, scenarios: Sequence[FaultScenario]) -> None:
    descriptions = {scenario.name: scenario.description for scenario in scenarios}
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        description = descriptions.get(result.name, "")
        print(f"[{status}] {result.name}: {description}")
        if not result.passed:
            print(f"    {result.detail.splitlines()[0]}", file=sys.stderr)
    total = len(report.results)
    passed = sum(1 for result in report.results if result.passed)
    print(f"{passed}/{total} scenarios passed")


def main_for(scenarios: Sequence[FaultScenario]) -> int:
    report = run_scenarios(scenarios)
    print_report(report, scenarios=scenarios)
    return 0 if report.ok else 1


__all__ = [
    "FaultReport",
    "FaultScenario",
    "ScenarioResult",
    "main_for",
    "print_report",
    "run_scenarios",
]
