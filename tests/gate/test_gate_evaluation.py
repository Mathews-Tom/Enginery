"""Tests for enginery.evaluation.gate: the pure gate-G4 evaluation over
already-derived metrics."""

from __future__ import annotations

from enginery.evaluation.gate import ConditionStatus, G4Inputs, evaluate_g4
from enginery.evaluation.gate_floor import GateFloorConfig
from enginery.evaluation.outcomes import CompletenessReport

_EMPTY_FLOOR = GateFloorConfig(
    schema_version=1,
    registered_principal_ids=(),
    completed_run_volume_floor=None,
    intervention_volume_floor=None,
    outcome_completeness_floor=None,
)

_ZERO_COMPLETENESS = CompletenessReport(
    derivation_version=1, captured=0, indeterminate=0, pending=0, completeness=1.0
)


def _inputs(**overrides: object) -> G4Inputs:
    defaults: dict[str, object] = {
        "completed_run_count": 0,
        "completed_workflow_type_count": 0,
        "completed_risk_class_count": 0,
        "intervention_with_reason_count": 0,
        "completeness": _ZERO_COMPLETENESS,
        "repository_count": 0,
    }
    defaults.update(overrides)
    return G4Inputs(**defaults)  # type: ignore[arg-type]


def _condition(report: object, condition_id: str) -> object:
    for condition in report.conditions:  # type: ignore[attr-defined]
        if condition.id == condition_id:
            return condition
    raise AssertionError(f"no condition {condition_id!r} in report")


def test_g4_report_has_exactly_six_conditions_in_registered_order() -> None:
    report = evaluate_g4(floor=_EMPTY_FLOOR, inputs=_inputs())

    assert [condition.id for condition in report.conditions] == [
        "completed_run_diversity",
        "human_intervention_volume",
        "outcome_capture_completeness",
        "recurring_evidence_backed_deficiency",
        "corpus_diversity",
        "registered_human_principals",
    ]


def test_gate_fails_overall_when_every_condition_is_unmeasured_or_failing() -> None:
    report = evaluate_g4(floor=_EMPTY_FLOOR, inputs=_inputs())

    assert report.passed is False


def test_completed_run_diversity_is_unmeasured_without_a_registered_floor() -> None:
    report = evaluate_g4(
        floor=_EMPTY_FLOOR,
        inputs=_inputs(
            completed_run_count=100,
            completed_workflow_type_count=5,
            completed_risk_class_count=5,
        ),
    )

    condition = _condition(report, "completed_run_diversity")
    assert condition.status is ConditionStatus.UNMEASURED  # type: ignore[union-attr]


def test_completed_run_diversity_fails_when_floor_registered_but_breadth_is_narrow() -> None:
    floor = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=(),
        completed_run_volume_floor=1,
        intervention_volume_floor=None,
        outcome_completeness_floor=None,
    )
    report = evaluate_g4(
        floor=floor,
        inputs=_inputs(
            completed_run_count=10,
            completed_workflow_type_count=1,
            completed_risk_class_count=1,
        ),
    )

    condition = _condition(report, "completed_run_diversity")
    assert condition.status is ConditionStatus.FAIL  # type: ignore[union-attr]


def test_completed_run_diversity_passes_when_floor_and_breadth_are_both_met() -> None:
    floor = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=(),
        completed_run_volume_floor=5,
        intervention_volume_floor=None,
        outcome_completeness_floor=None,
    )
    report = evaluate_g4(
        floor=floor,
        inputs=_inputs(
            completed_run_count=5,
            completed_workflow_type_count=2,
            completed_risk_class_count=2,
        ),
    )

    condition = _condition(report, "completed_run_diversity")
    assert condition.status is ConditionStatus.PASS  # type: ignore[union-attr]


def test_human_intervention_volume_is_unmeasured_without_a_registered_floor() -> None:
    report = evaluate_g4(floor=_EMPTY_FLOOR, inputs=_inputs(intervention_with_reason_count=50))

    condition = _condition(report, "human_intervention_volume")
    assert condition.status is ConditionStatus.UNMEASURED  # type: ignore[union-attr]


def test_human_intervention_volume_passes_at_the_registered_floor() -> None:
    floor = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=(),
        completed_run_volume_floor=None,
        intervention_volume_floor=10,
        outcome_completeness_floor=None,
    )
    report = evaluate_g4(floor=floor, inputs=_inputs(intervention_with_reason_count=10))

    condition = _condition(report, "human_intervention_volume")
    assert condition.status is ConditionStatus.PASS  # type: ignore[union-attr]


def test_outcome_capture_completeness_is_unmeasured_without_a_registered_floor() -> None:
    report = evaluate_g4(
        floor=_EMPTY_FLOOR,
        inputs=_inputs(
            completeness=CompletenessReport(
                derivation_version=1,
                captured=9,
                indeterminate=1,
                pending=0,
                completeness=0.9,
            )
        ),
    )

    condition = _condition(report, "outcome_capture_completeness")
    assert condition.status is ConditionStatus.UNMEASURED  # type: ignore[union-attr]


def test_outcome_capture_completeness_fails_below_the_registered_floor() -> None:
    floor = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=(),
        completed_run_volume_floor=None,
        intervention_volume_floor=None,
        outcome_completeness_floor=0.8,
    )
    report = evaluate_g4(
        floor=floor,
        inputs=_inputs(
            completeness=CompletenessReport(
                derivation_version=1,
                captured=7,
                indeterminate=3,
                pending=0,
                completeness=0.7,
            )
        ),
    )

    condition = _condition(report, "outcome_capture_completeness")
    assert condition.status is ConditionStatus.FAIL  # type: ignore[union-attr]


def test_recurring_evidence_backed_deficiency_is_always_unmeasured() -> None:
    report = evaluate_g4(
        floor=_EMPTY_FLOOR,
        inputs=_inputs(
            completed_run_count=1000,
            completed_workflow_type_count=5,
            completed_risk_class_count=5,
            intervention_with_reason_count=1000,
            repository_count=1000,
        ),
    )

    condition = _condition(report, "recurring_evidence_backed_deficiency")
    assert condition.status is ConditionStatus.UNMEASURED  # type: ignore[union-attr]


def test_corpus_diversity_fails_at_one_repository() -> None:
    report = evaluate_g4(floor=_EMPTY_FLOOR, inputs=_inputs(repository_count=1))

    condition = _condition(report, "corpus_diversity")
    assert condition.status is ConditionStatus.FAIL  # type: ignore[union-attr]


def test_corpus_diversity_passes_at_two_repositories() -> None:
    report = evaluate_g4(floor=_EMPTY_FLOOR, inputs=_inputs(repository_count=2))

    condition = _condition(report, "corpus_diversity")
    assert condition.status is ConditionStatus.PASS  # type: ignore[union-attr]


def test_registered_human_principals_fails_at_zero_and_one() -> None:
    zero = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=(),
        completed_run_volume_floor=None,
        intervention_volume_floor=None,
        outcome_completeness_floor=None,
    )
    one = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=("operator-a",),
        completed_run_volume_floor=None,
        intervention_volume_floor=None,
        outcome_completeness_floor=None,
    )

    assert (
        _condition(evaluate_g4(floor=zero, inputs=_inputs()), "registered_human_principals").status  # type: ignore[union-attr]
        is ConditionStatus.FAIL
    )
    assert (
        _condition(evaluate_g4(floor=one, inputs=_inputs()), "registered_human_principals").status  # type: ignore[union-attr]
        is ConditionStatus.FAIL
    )


def test_registered_human_principals_passes_at_two() -> None:
    two = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=("operator-a", "operator-b"),
        completed_run_volume_floor=None,
        intervention_volume_floor=None,
        outcome_completeness_floor=None,
    )

    report = evaluate_g4(floor=two, inputs=_inputs())

    condition = _condition(report, "registered_human_principals")
    assert condition.status is ConditionStatus.PASS  # type: ignore[union-attr]


def test_gate_passes_overall_only_when_every_condition_passes() -> None:
    floor = GateFloorConfig(
        schema_version=1,
        registered_principal_ids=("operator-a", "operator-b"),
        completed_run_volume_floor=5,
        intervention_volume_floor=1,
        outcome_completeness_floor=0.5,
    )
    inputs = _inputs(
        completed_run_count=5,
        completed_workflow_type_count=2,
        completed_risk_class_count=2,
        intervention_with_reason_count=1,
        completeness=CompletenessReport(
            derivation_version=1, captured=5, indeterminate=0, pending=0, completeness=1.0
        ),
        repository_count=2,
    )

    report = evaluate_g4(floor=floor, inputs=inputs)

    # Even with five of six conditions passing, the always-unmeasured
    # recurring-deficiency condition still blocks the gate as a whole.
    assert report.passed is False
    assert (
        _condition(report, "recurring_evidence_backed_deficiency").status  # type: ignore[union-attr]
        is ConditionStatus.UNMEASURED
    )
