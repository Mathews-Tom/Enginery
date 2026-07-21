"""Tests for the cumulative Stage-1/Stage-2 `full_system_gate.py` gate.

Imports the script directly (`pythonpath = [".", "scripts"]` in
`pyproject.toml`), matching the existing convention for
`tests/governance/test_adversarial_gates.py`. Runs the real gate logic --
not a separate reimplementation -- so a regression in the gate itself
fails CI rather than only a manual release-gate invocation.
"""

from __future__ import annotations

import pytest

from enginery.domain.errors import EngineryError
from full_system_gate import run_gate


def test_stage1_only_gate_passes() -> None:
    report = run_gate(stages="1", restart_between_stages=True)
    assert len(report.run_evidence) == 2
    assert report.stage2_evidence is None
    assert report.evidence_digest.startswith("sha256:")


def test_stage2_only_gate_passes() -> None:
    report = run_gate(stages="2", restart_between_stages=True)
    assert report.run_evidence == []
    assert report.stage2_evidence is not None
    assert report.stage2_evidence["distribution_name"] == "enginery-full-system-gate-fixture"
    assert report.stage2_evidence["merged_slices"] == [201, 202]
    assert report.stage2_evidence["github_release_count"] == 1
    assert report.stage2_evidence["restart_between_stages"] is True


def test_stage2_gate_passes_without_restart_between_stages() -> None:
    report = run_gate(stages="2", restart_between_stages=False)
    assert report.stage2_evidence is not None
    assert report.stage2_evidence["restart_between_stages"] is False


def test_cumulative_stage1_and_stage2_gate_passes() -> None:
    report = run_gate(stages="1,2", restart_between_stages=True)
    assert len(report.run_evidence) == 2
    assert report.stage2_evidence is not None


def test_unsupported_stage_is_rejected() -> None:
    with pytest.raises(EngineryError):
        run_gate(stages="3")


def test_blank_stages_is_rejected() -> None:
    with pytest.raises(EngineryError):
        run_gate(stages="")
