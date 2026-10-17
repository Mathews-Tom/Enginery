"""A legacy empty-contract PASS cannot authorize behavioral acceptance."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import pytest

from enginery.application.behavioral_checks import BehavioralCheck
from enginery.domain.behavioral_evidence import ReceiptCategory
from enginery.domain.evidence import EvidenceItem
from enginery.domain.node_attempt import EvidenceResult
from enginery.evidence.evaluator import EvidenceContract, EvidenceEvaluation, EvidenceEvaluator
from tests.evidence.test_behavioral_acceptance import (
    _current,
    _repository,
    _run,
    _service,
)


def test_zero_check_acceptance_cannot_reach_legacy_empty_requirements_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[EvidenceContract] = []

    def legacy_empty_pass(
        self: EvidenceEvaluator,
        contract: EvidenceContract,
        evidence_items: Iterable[EvidenceItem],
        reference_time: datetime,
    ) -> EvidenceEvaluation:
        calls.append(contract)
        return EvidenceEvaluation(
            result=EvidenceResult.PASS,
            satisfied=(),
            missing=(),
            failed=(),
            indeterminate=(),
            stale=(),
            subject_mismatch=(),
        )

    monkeypatch.setattr(EvidenceEvaluator, "evaluate", legacy_empty_pass)
    checkout, head = _repository(tmp_path, "print('unused')\n")
    checks: tuple[BehavioralCheck, ...] = ()
    inputs = _current(checks, checkout, head)
    # Exercise the public service and durable verdict, not just the pure evaluator.
    result = _run(_service(tmp_path), checks, inputs, checkout)
    assert calls == []
    assert result.result is EvidenceResult.INDETERMINATE
    assert result.receipt.category is ReceiptCategory.EVALUATED_VERDICT
    assert result.receipt.assertion_outcomes == ()
    assert "no_behavioral_checks" in result.holds
