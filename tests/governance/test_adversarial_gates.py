from __future__ import annotations

from adversarial_merge_ready_gate import run_gate as run_merge_ready_gate
from adversarial_policy_gate import run_gate as run_policy_gate


def test_generated_authority_bypasses_are_rejected() -> None:
    run_policy_gate()


def test_generated_evidence_bypasses_are_rejected() -> None:
    run_merge_ready_gate()
