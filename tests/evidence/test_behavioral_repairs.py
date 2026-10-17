"""Regression coverage for current-build semantics and fresh output custody."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from enginery.application.behavioral_checks import BehavioralAssertion, BehavioralCheck
from enginery.domain.errors import StaleEvidenceError
from enginery.domain.node_attempt import EvidenceResult
from tests.evidence.test_behavioral_acceptance import (
    _calibrate,
    _current,
    _repository,
    _run,
    _service,
)


def test_wrong_build_only_cannot_qualify_skipped_exit_zero(tmp_path: Path) -> None:
    checkout, head = _repository(tmp_path, "print('1 skipped')\n")
    check = BehavioralCheck(
        "skipped",
        "command",
        (BehavioralAssertion("exit_status", "equals", 0),),
        ("wrong_build",),
        argv=(sys.executable, "-I", "behavior.py"),
    )
    inputs = _current((check,), checkout, head)
    service = _service(tmp_path)
    calibration = _calibrate(service, (check,), inputs, checkout)
    assert calibration.results[0].classification == "caught"
    result = _run(service, (check,), inputs, checkout)
    assert result.result is EvidenceResult.INDETERMINATE
    assert "behavioral_calibration_semantic_catch_required" in result.holds


def test_committed_output_is_refused_before_candidate_execution(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    checkout, _ = _repository(
        tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('5')\n"
    )
    (checkout / "result.txt").write_text("5")
    subprocess.run(("git", "add", "result.txt"), cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "output",
        ),
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=checkout, text=True).strip()
    check = BehavioralCheck(
        "persist",
        "command",
        (BehavioralAssertion("produced_file", "equals", "5", path="result.txt"),),
        ("skipped_persistence", "wrong_build"),
        argv=(sys.executable, "-I", "behavior.py"),
    )
    service = _service(tmp_path)
    inputs = _current((check,), checkout, head)
    calibration = _calibrate(service, (check,), inputs, checkout)
    assert all(item.baseline_result is EvidenceResult.INDETERMINATE for item in calibration.results)
    result = _run(service, (check,), inputs, checkout)
    assert result.result is EvidenceResult.INDETERMINATE
    assert "behavioral_output_preexists" in result.holds
    assert not marker.exists()


def test_current_calibration_regression_reports_failed_assertion(tmp_path: Path) -> None:
    control = tmp_path / "result"
    control.write_text("changed")
    checkout, head = _repository(
        tmp_path, f"from pathlib import Path\nprint(Path({str(control)!r}).read_text(), end='')\n"
    )
    check = BehavioralCheck(
        "behavior",
        "command",
        (BehavioralAssertion("stdout", "equals", "changed"),),
        ("constant_result", "wrong_build"),
        argv=(sys.executable, "-I", "behavior.py"),
    )
    inputs = _current((check,), checkout, head)
    service = _service(tmp_path)
    _calibrate(service, (check,), inputs, checkout)
    assert _run(service, (check,), inputs, checkout).result is EvidenceResult.PASS
    control.write_text("regressed")
    result = _run(service, (check,), inputs, checkout)
    assert result.result is EvidenceResult.FAIL
    assert result.holds == ("behavioral_assertion_failed:behavior:0",)


def test_checkout_mutation_during_observation_refuses_receipt(tmp_path: Path) -> None:
    checkout, head = _repository(
        tmp_path,
        "from pathlib import Path\nPath('behavior.py').write_text('mutated')\nprint('changed')\n",
    )
    check = BehavioralCheck(
        "mutation",
        "command",
        (BehavioralAssertion("stdout", "contains", "changed"),),
        ("constant_result",),
        argv=(sys.executable, "-I", "behavior.py"),
    )
    service = _service(tmp_path)
    with pytest.raises(StaleEvidenceError, match="behavioral_build_mismatch"):
        _run(service, (check,), _current((check,), checkout, head), checkout)
