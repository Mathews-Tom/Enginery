"""Frozen bootstrap faults execute the real local behavioral calibration driver."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import factory_gate_bootstrap_calibration as calibration
from enginery.evidence.bootstrap_calibration import load_calibration_set
from factory_gate_bootstrap_calibration import run_calibration


def test_frozen_calibration_executes_all_four_outcomes(tmp_path: Path) -> None:
    dataset = load_calibration_set()
    assert len(dataset["entries"]) >= 4
    assert all(entry["owner"] == "factory-maintainers" for entry in dataset["entries"])
    report = run_calibration(tmp_path)
    assert report["passed"], report
    assert set(report["outcomes"]) == {"caught", "escaped", "not_injected", "inconclusive"}
    weak = next(entry for entry in report["entries"] if entry["id"] == "weak-exit-status")
    assert [item["result"]["classification"] for item in weak["receipts"]] == ["escaped", "caught"]
    assert [item["assertion"] for item in weak["receipts"]] == ["exit_status", "stdout"]
    assert weak["receipts"][0]["result"]["rung"] is None
    assert weak["receipts"][1]["result"]["rung"] == "evaluator"
    assert any("/artifacts/" in name for name in report["digests"])
    for name, digest in report["digests"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    for entry in report["entries"]:
        assert entry["outcomes"] == entry["expected_outcomes"]
        assert entry["rung"] == entry["receipts"][0]["result"]["rung"]
        assert entry["classification"] == entry["receipts"][0]["result"]["classification"]
        for receipt in entry["receipts"]:
            assert receipt["artifacts"]
            for digest, name in receipt["artifacts"].items():
                assert (
                    f"sha256:{hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}" == digest
                )
                assert name in report["digests"]
            for key in ("baseline_receipt_digest", "observation_receipt_digest"):
                digest = receipt["result"].get(key)
                if digest is not None:
                    assert digest in receipt["artifacts"]


@pytest.fixture
def recorded_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the product result independent from the expected dataset labels."""

    class RecordedGate:
        def __init__(self, options: Any, evidence: Any) -> None:
            self.options = options
            self.root = tmp_path
            self.check: dict[str, Any] = {}

        def bind(self, checks: list[dict[str, Any]]) -> str:
            self.check = checks[0]
            return self.check["name"]

        def factory(self, *args: str) -> tuple[int, dict[str, Any]]:
            classification, rung = {
                ("constant-output", "stdout"): ("caught", "evaluator"),
                ("weak-exit-status", "exit_status"): ("escaped", None),
                ("weak-exit-status", "stdout"): ("caught", "evaluator"),
                ("withheld-output", "stdout"): ("not_injected", None),
                ("missing-observer", "stdout"): ("inconclusive", None),
            }[(self.check["name"], self.check["assertions"][0]["kind"])]
            return 0, {
                "calibration": {
                    "results": [
                        {
                            "fault_class": "constant_result",
                            "classification": classification,
                            "rung": rung,
                        }
                    ]
                }
            }

        def cleanup(self) -> None:
            pass

    monkeypatch.setattr(calibration, "BehavioralGate", RecordedGate)
    monkeypatch.setattr(calibration, "_retain_artifacts", lambda *_args: {})


@pytest.mark.parametrize(
    ("entry_id", "field", "unexpected"),
    [
        ("constant-output", "rung", "observer"),
        ("constant-output", "expected_outcome", "escaped"),
        ("constant-output", "assertion", "exit_status"),
        ("weak-exit-status", "rung", "observer"),
        ("weak-exit-status", "expected_outcome", "caught"),
        ("weak-exit-status", "assertion", "stdout"),
        ("weak-exit-status", "next_rung", "observer"),
        ("weak-exit-status", "next_expected_outcome", "escaped"),
        ("weak-exit-status", "next_assertion", "exit_status"),
        ("withheld-output", "rung", "evaluator"),
        ("withheld-output", "expected_outcome", "caught"),
        ("withheld-output", "assertion", "exit_status"),
        ("missing-observer", "rung", "observer"),
        ("missing-observer", "expected_outcome", "caught"),
        ("missing-observer", "assertion", "exit_status"),
    ],
)
def test_every_expected_rung_outcome_and_assertion_is_compared(
    recorded_driver: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry_id: str,
    field: str,
    unexpected: str,
) -> None:
    dataset = load_calibration_set()
    entry = next(item for item in dataset["entries"] if item["id"] == entry_id)
    entry[field] = unexpected
    monkeypatch.setattr(calibration, "load_calibration_set", lambda: dataset)

    report = run_calibration(tmp_path)

    assert not report["passed"]
    assert [item["id"] for item in report["entries"] if not item["passed"]] == [entry_id]
    assert not next(item for item in report["summary"] if item["id"] == entry_id)["passed"]


def test_calibration_summary_is_stable_and_reports_product_results(
    recorded_driver: None, tmp_path: Path
) -> None:
    first = run_calibration(tmp_path / "first")
    second = run_calibration(tmp_path / "second")

    assert first["entries"] != second["entries"]
    assert first["summary"] == second["summary"]
    assert first["passed"] and second["passed"]
    assert first["summary"] == [
        {
            "id": "constant-output",
            "outcomes": [{"outcome": "caught", "rung": "evaluator", "assertion": "stdout"}],
            "passed": True,
        },
        {
            "id": "weak-exit-status",
            "outcomes": [
                {"outcome": "escaped", "rung": None, "assertion": "exit_status"},
                {"outcome": "caught", "rung": "evaluator", "assertion": "stdout"},
            ],
            "passed": True,
        },
        {
            "id": "withheld-output",
            "outcomes": [{"outcome": "not_injected", "rung": None, "assertion": "stdout"}],
            "passed": True,
        },
        {
            "id": "missing-observer",
            "outcomes": [{"outcome": "inconclusive", "rung": None, "assertion": "stdout"}],
            "passed": True,
        },
    ]
