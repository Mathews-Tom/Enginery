from __future__ import annotations

from datetime import UTC, datetime, timedelta

from enginery.domain.evidence import EvidenceItem
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.evidence.evaluator import (
    EvidenceContract,
    EvidenceEvaluator,
    EvidenceRequirement,
)


def _agent() -> AuthorityPrincipal:
    return AuthorityPrincipal("agent-1", PrincipalType.AGENT, "worker", "fixture")


def _item(
    *,
    result: EvidenceResult,
    observed_time: datetime,
    subject_revision: str = "head-1",
    validity_window_seconds: int = 3600,
) -> EvidenceItem:
    return EvidenceItem(
        type="ci",
        schema_version=1,
        producer=_agent(),
        subject_revision=subject_revision,
        observed_time=observed_time,
        validity_window_seconds=validity_window_seconds,
        result=result,
    )


def _contract() -> EvidenceContract:
    return EvidenceContract((EvidenceRequirement("current-ci", "ci", "head-1"),))


def test_evidence_evaluator_passes_current_passing_requirement() -> None:
    now = datetime.now(UTC)

    evaluation = EvidenceEvaluator().evaluate(
        _contract(),
        (_item(result=EvidenceResult.PASS, observed_time=now),),
        now,
    )

    assert evaluation.result is EvidenceResult.PASS
    assert evaluation.satisfied == ("current-ci",)
    assert evaluation.blocks_terminal is False


def test_missing_hard_required_evidence_is_indeterminate_and_blocks() -> None:
    now = datetime.now(UTC)

    evaluation = EvidenceEvaluator().evaluate(_contract(), (), now)

    assert evaluation.result is EvidenceResult.INDETERMINATE
    assert evaluation.missing == ("current-ci",)
    assert evaluation.blocks_terminal is True


def test_indeterminate_required_evidence_blocks_without_becoming_pass() -> None:
    now = datetime.now(UTC)

    evaluation = EvidenceEvaluator().evaluate(
        _contract(),
        (_item(result=EvidenceResult.INDETERMINATE, observed_time=now),),
        now,
    )

    assert evaluation.result is EvidenceResult.INDETERMINATE
    assert evaluation.indeterminate == ("current-ci",)


def test_failed_required_evidence_is_fail() -> None:
    now = datetime.now(UTC)

    evaluation = EvidenceEvaluator().evaluate(
        _contract(),
        (_item(result=EvidenceResult.FAIL, observed_time=now),),
        now,
    )

    assert evaluation.result is EvidenceResult.FAIL
    assert evaluation.failed == ("current-ci",)


def test_stale_required_evidence_is_fail() -> None:
    now = datetime.now(UTC)

    evaluation = EvidenceEvaluator().evaluate(
        _contract(),
        (
            _item(
                result=EvidenceResult.PASS,
                observed_time=now - timedelta(seconds=61),
                validity_window_seconds=60,
            ),
        ),
        now,
    )

    assert evaluation.result is EvidenceResult.FAIL
    assert evaluation.stale == ("current-ci",)


def test_wrong_subject_evidence_is_fail() -> None:
    now = datetime.now(UTC)

    evaluation = EvidenceEvaluator().evaluate(
        _contract(),
        (_item(result=EvidenceResult.PASS, observed_time=now, subject_revision="head-2"),),
        now,
    )

    assert evaluation.result is EvidenceResult.FAIL
    assert evaluation.subject_mismatch == ("current-ci",)
