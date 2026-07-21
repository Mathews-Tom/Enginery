#!/usr/bin/env python3
"""Measure and assert local performance bounds for the Stage-1 path only.

Every number this script prints is a real, executed measurement on the
current machine -- never a placeholder or speculative figure. It fails
closed (non-zero exit, no bound silently treated as passing) whenever a
required bound is missing from ``--assert-bounds`` or a measurement
cannot execute, per the system design's explicit rule that performance
claims require a measured baseline and no speculative throughput claim
belongs in release docs.

Stage 2-4 performance is out of scope: this milestone measures only the
Stage-1 issue-to-merge-ready path, one SQLite ledger append throughput,
and one ``ledger verify`` pass, matching the ``v0.1.0`` train's own
Stage-1-only cumulative gate.
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from enginery.cli.ledger import run_verify
from enginery.domain.errors import EngineryError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from full_system_gate import run_gate

_LEDGER_APPEND_EVENTS = 500


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    value: float
    unit: str


def _measure_stage1_full_lifecycle() -> Measurement:
    started = time.perf_counter()
    run_gate(stages="1", restart_between_stages=True)
    elapsed = time.perf_counter() - started
    return Measurement("stage1_full_lifecycle", elapsed, "seconds")


def _measure_ledger_append_throughput(database: Path) -> Measurement:
    ledger = LedgerService.open(database)
    try:
        started = time.perf_counter()
        for index in range(_LEDGER_APPEND_EVENTS):
            ledger.append(
                AppendCommand(
                    correlation_id=f"perf-append-{index}",
                    events=(
                        EventWrite(
                            aggregate_type="perf_probe",
                            aggregate_id=f"probe-{index}",
                            expected_version=0,
                            event_type="perf_probe.appended",
                            schema_version=1,
                            payload={"index": index},
                        ),
                    ),
                )
            )
        elapsed = time.perf_counter() - started
    finally:
        ledger.close()
    events_per_second = _LEDGER_APPEND_EVENTS / elapsed if elapsed > 0 else float("inf")
    return Measurement("ledger_append", events_per_second, "events_per_second")


def _measure_ledger_verify(database: Path) -> Measurement:
    started = time.perf_counter()
    report = run_verify(database=database, artifacts=None)
    elapsed = time.perf_counter() - started
    if not report.healthy:
        raise EngineryError(
            "ledger verify reported an inconsistency during the performance measurement",
            details={"issues": [str(issue) for issue in report.issues]},
        )
    return Measurement("ledger_verify", elapsed, "seconds")


def measure_all() -> list[Measurement]:
    measurements = [_measure_stage1_full_lifecycle()]
    with TemporaryDirectory(prefix="enginery-performance-baseline-") as tmp:
        database = Path(tmp) / "ledger.db"
        measurements.append(_measure_ledger_append_throughput(database))
        measurements.append(_measure_ledger_verify(database))
    return measurements


def _load_bounds(path: Path) -> dict[str, dict[str, float]]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise EngineryError(
            "performance bounds file could not be read", details={"path": str(path)}
        ) from error
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise EngineryError(
            "performance bounds file is not valid TOML", details={"path": str(path)}
        ) from error
    bounds: dict[str, dict[str, float]] = {}
    for section, values in data.items():
        if not isinstance(values, dict):
            raise EngineryError(
                "performance bounds section must be a table",
                details={"section": section},
            )
        bounds[section] = {key: float(value) for key, value in values.items()}
    return bounds


def _check(measurement: Measurement, bounds: dict[str, dict[str, float]]) -> tuple[bool, str]:
    section = bounds.get(measurement.name)
    if section is None:
        return False, f"{measurement.name}: no bound section declared in the bounds file"
    if "max_seconds" in section:
        bound = section["max_seconds"]
        ok = measurement.value <= bound
        detail = (
            f"{measurement.name}={measurement.value:.4f}{measurement.unit[:1]} "
            f"bound<={bound:.4f}s: {'PASS' if ok else 'FAIL'}"
        )
        return ok, detail
    if "min_events_per_second" in section:
        bound = section["min_events_per_second"]
        ok = measurement.value >= bound
        detail = (
            f"{measurement.name}={measurement.value:.1f}{measurement.unit} "
            f"bound>={bound:.1f}: {'PASS' if ok else 'FAIL'}"
        )
        return ok, detail
    return False, f"{measurement.name}: bound section declares no recognized key"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure and assert local Stage-1 performance bounds."
    )
    parser.add_argument("--assert-bounds", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        bounds = _load_bounds(args.assert_bounds)
        measurements = measure_all()
    except EngineryError as error:
        print(f"FAIL performance-baseline: {error}", file=sys.stderr)
        return 1
    all_passed = True
    for measurement in measurements:
        ok, detail = _check(measurement, bounds)
        print(detail)
        all_passed = all_passed and ok
    if not all_passed:
        print("FAIL performance-baseline: one or more measurements exceeded their bound")
        return 1
    print("PASS performance-baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
