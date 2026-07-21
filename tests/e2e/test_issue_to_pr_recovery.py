"""Fast pytest wrapper around the cumulative Stage-1 restart/replay gate.

Delegates to ``scripts/full_system_gate.py`` (importable through
``pyproject.toml``'s ``pythonpath = [".", "scripts"]``) so the CI-run
suite and the standalone
``uv run python scripts/full_system_gate.py --stages 1
--restart-between-stages`` gate exercise the exact same code, matching
this repository's existing convention of test suites importing scripts
(for example ``tests/governance/test_adversarial_gates.py`` importing
``adversarial_merge_ready_gate``).
"""

from __future__ import annotations

from enginery.domain.errors import EngineryError
from full_system_gate import run_gate


def test_stage1_cumulative_restart_and_replay_reaches_merge_ready_twice() -> None:
    report = run_gate(stages="1", restart_between_stages=True)

    assert report.stages == "1"
    assert report.restart_between_stages is True
    assert len(report.run_evidence) == 2
    assert report.evidence_digest.startswith("sha256:")
    for evidence in report.run_evidence:
        assert evidence["status"] == "created"
        assert evidence["pull_request_requests"] == 1

    run_ids = {str(evidence["run_id"]) for evidence in report.run_evidence}
    assert len(run_ids) == 2, "cumulative runs must not collide on one durable ledger"


def test_stage1_gate_rejects_unsupported_stages() -> None:
    try:
        run_gate(stages="1,2")
    except EngineryError as error:
        assert "only supports --stages 1" in str(error)
    else:
        raise AssertionError("Stage 2-4 cumulative gates must be rejected in this milestone")


def test_stage1_gate_without_restart_still_proves_cumulative_correctness() -> None:
    report = run_gate(stages="1", restart_between_stages=False)

    assert report.restart_between_stages is False
    assert len(report.run_evidence) == 2
