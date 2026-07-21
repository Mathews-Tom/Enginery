"""Tests for the live Stage 3 `run_stage3_gate.py` gate.

Imports the script directly (`pythonpath = [".", "scripts"]` in
`pyproject.toml`), matching `tests/system/test_full_system_gate.py`'s
convention. Unlike Stage 2's live GitHub/PyPI gate, this touches only a
local process and no external credential, so it is not opt-in gated:
runs as part of ordinary CI, proving the real gate logic on every PR
rather than only a manual invocation.
"""

from __future__ import annotations

from enginery.domain.incident import IncidentState
from run_stage3_gate import run_gate


def test_live_gate_deploys_hotfix_observes_rolls_back_and_restores() -> None:
    report = run_gate()

    assert report.regression_non_vacuous is True
    assert report.deployed_revision == report.observed_before_rollback
    assert report.rolled_back is True
    assert report.restored_revision == report.release_lineage.affected_revision
    assert report.restored_revision != report.deployed_revision
    assert report.authority_record_count == 2
    assert report.follow_up_work_item_id.startswith("incident-follow-up:")
    assert report.final_state == IncidentState.ROLLED_BACK.value
