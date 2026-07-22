"""Deterministic readiness reporting against a registered decision gate.

Pure evaluation over already-computed metrics: every value this module
consumes is derived elsewhere from durable ledger state and a registered
floor configuration (see :mod:`enginery.evaluation.gate_floor`). This
module owns no I/O and reads no ledger projection itself, keeping the
gate-status contract testable without a ledger fixture and free of any
dependency on ``enginery.engine`` or ``enginery.workflows`` (see
``scripts/check_import_boundaries.py``'s ``evaluation`` layer rule).

A condition whose registered floor is unset reports ``unmeasured``: this
module never substitutes a default floor, and never reports ``pass`` for
a condition it cannot actually measure from what is passed in. Gate G4's
six registered conditions are docs/design.md's evaluation/gate-G4 entry
gate; :func:`evaluate_g4` reports exactly one line per condition, no
more, no fewer, and the gate as a whole only ``passed`` when every one
of those six lines is ``pass``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass

from enginery.evaluation.gate_floor import GateFloorConfig
from enginery.evaluation.outcomes import CompletenessReport

_MIN_REPOSITORY_DIVERSITY = 2
_MIN_REGISTERED_PRINCIPALS = 2
_MIN_WORKFLOW_TYPE_DIVERSITY = 2
_MIN_RISK_CLASS_DIVERSITY = 2


class ConditionStatus(enum.Enum):
    """The three states one gate condition can report. There is no
    fourth value: a condition this instrument cannot measure from
    already-captured data reports ``UNMEASURED``, never ``PASS``."""

    PASS = "pass"
    FAIL = "fail"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True, slots=True)
class GateCondition:
    """One reported line: exactly one registered gate condition."""

    id: str
    status: ConditionStatus
    detail: str
    metrics: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GateReport:
    """The full readiness report for one gate."""

    gate: str
    conditions: tuple[GateCondition, ...]

    @property
    def passed(self) -> bool:
        """The gate as a whole passes only when every condition does --
        an ``unmeasured`` condition blocks the gate exactly like a
        ``fail``, never silently treated as satisfied."""
        return all(condition.status is ConditionStatus.PASS for condition in self.conditions)


@dataclass(frozen=True, slots=True)
class G4Inputs:
    """Every durable measurement :func:`evaluate_g4` needs, already
    derived by the caller from ledger state. Kept as one bundle so the
    evaluation signature stays stable as new callers appear."""

    completed_run_count: int
    completed_workflow_type_count: int
    completed_risk_class_count: int
    intervention_with_reason_count: int
    completeness: CompletenessReport
    repository_count: int


def evaluate_g4(*, floor: GateFloorConfig, inputs: G4Inputs) -> GateReport:
    """The six registered gate-G4 entry conditions, each its own reported
    line. See the module docstring for the fail-closed contract."""
    conditions = (
        _completed_run_diversity(floor, inputs),
        _human_intervention_volume(floor, inputs),
        _outcome_capture_completeness(floor, inputs),
        _recurring_workflow_deficiency(),
        _corpus_diversity(inputs),
        _registered_human_principals(floor),
    )
    return GateReport(gate="G4", conditions=conditions)


def _completed_run_diversity(floor: GateFloorConfig, inputs: G4Inputs) -> GateCondition:
    metrics: dict[str, object] = {
        "completed_run_count": inputs.completed_run_count,
        "completed_workflow_type_count": inputs.completed_workflow_type_count,
        "completed_risk_class_count": inputs.completed_risk_class_count,
        "registered_volume_floor": floor.completed_run_volume_floor,
    }
    if floor.completed_run_volume_floor is None:
        return GateCondition(
            id="completed_run_diversity",
            status=ConditionStatus.UNMEASURED,
            detail=(
                "no completed-run volume floor is registered yet (set at the first "
                "quarterly gate review); breadth today is "
                f"{inputs.completed_workflow_type_count} workflow type(s) and "
                f"{inputs.completed_risk_class_count} risk class(es) across "
                f"{inputs.completed_run_count} completed run(s)"
            ),
            metrics=metrics,
        )
    breadth_met = (
        inputs.completed_workflow_type_count >= _MIN_WORKFLOW_TYPE_DIVERSITY
        and inputs.completed_risk_class_count >= _MIN_RISK_CLASS_DIVERSITY
    )
    volume_met = inputs.completed_run_count >= floor.completed_run_volume_floor
    status = ConditionStatus.PASS if breadth_met and volume_met else ConditionStatus.FAIL
    return GateCondition(
        id="completed_run_diversity",
        status=status,
        detail=(
            f"{inputs.completed_run_count} completed run(s) across "
            f"{inputs.completed_workflow_type_count} workflow type(s) and "
            f"{inputs.completed_risk_class_count} risk class(es); registered floor is "
            f"{floor.completed_run_volume_floor} completed run(s) with at least "
            f"{_MIN_WORKFLOW_TYPE_DIVERSITY} workflow types and "
            f"{_MIN_RISK_CLASS_DIVERSITY} risk classes"
        ),
        metrics=metrics,
    )


def _human_intervention_volume(floor: GateFloorConfig, inputs: G4Inputs) -> GateCondition:
    metrics: dict[str, object] = {
        "intervention_with_reason_count": inputs.intervention_with_reason_count,
        "registered_volume_floor": floor.intervention_volume_floor,
    }
    if floor.intervention_volume_floor is None:
        return GateCondition(
            id="human_intervention_volume",
            status=ConditionStatus.UNMEASURED,
            detail=(
                "no intervention-volume floor is registered yet; "
                f"{inputs.intervention_with_reason_count} recorded human intervention(s) "
                "with a reason exist today"
            ),
            metrics=metrics,
        )
    status = (
        ConditionStatus.PASS
        if inputs.intervention_with_reason_count >= floor.intervention_volume_floor
        else ConditionStatus.FAIL
    )
    return GateCondition(
        id="human_intervention_volume",
        status=status,
        detail=(
            f"{inputs.intervention_with_reason_count} recorded human intervention(s) with "
            f"a reason; registered floor is {floor.intervention_volume_floor}"
        ),
        metrics=metrics,
    )


def _outcome_capture_completeness(floor: GateFloorConfig, inputs: G4Inputs) -> GateCondition:
    metrics: dict[str, object] = {
        "completeness": inputs.completeness.completeness,
        "captured": inputs.completeness.captured,
        "indeterminate": inputs.completeness.indeterminate,
        "pending": inputs.completeness.pending,
        "derivation_version": inputs.completeness.derivation_version,
        "registered_floor": floor.outcome_completeness_floor,
    }
    if floor.outcome_completeness_floor is None:
        return GateCondition(
            id="outcome_capture_completeness",
            status=ConditionStatus.UNMEASURED,
            detail=(
                "no outcome-capture completeness floor is registered yet; the current "
                f"all-time completeness derivation reports {inputs.completeness.completeness:.4f} "
                f"over {inputs.completeness.captured} captured and "
                f"{inputs.completeness.indeterminate} indeterminate observation(s) -- this "
                "derivation has no trailing-window implementation yet, so a registered floor "
                "would still be checked against the all-time ratio, not a rolling window"
            ),
            metrics=metrics,
        )
    status = (
        ConditionStatus.PASS
        if inputs.completeness.completeness >= floor.outcome_completeness_floor
        else ConditionStatus.FAIL
    )
    return GateCondition(
        id="outcome_capture_completeness",
        status=status,
        detail=(
            f"all-time completeness {inputs.completeness.completeness:.4f} against registered "
            f"floor {floor.outcome_completeness_floor:.4f} (no trailing-window derivation "
            "exists yet; this compares the all-time captured/indeterminate ratio)"
        ),
        metrics=metrics,
    )


def _recurring_workflow_deficiency() -> GateCondition:
    return GateCondition(
        id="recurring_evidence_backed_deficiency",
        status=ConditionStatus.UNMEASURED,
        detail=(
            "this instrument has no mechanism to detect a recurring, evidence-backed "
            "workflow deficiency from captured data; identifying one is a human judgment "
            "call this command never reports pass or fail for"
        ),
        metrics={},
    )


def _corpus_diversity(inputs: G4Inputs) -> GateCondition:
    status = (
        ConditionStatus.PASS
        if inputs.repository_count >= _MIN_REPOSITORY_DIVERSITY
        else ConditionStatus.FAIL
    )
    repository_word = "repository" if inputs.repository_count == 1 else "repositories"
    return GateCondition(
        id="corpus_diversity",
        status=status,
        detail=(
            f"{inputs.repository_count} distinct {repository_word} observed across "
            "registered run history; external-adopter status has no tracked signal in "
            "this codebase and is not counted toward this condition"
        ),
        metrics={"repository_count": inputs.repository_count},
    )


def _registered_human_principals(floor: GateFloorConfig) -> GateCondition:
    count = len(floor.registered_principal_ids)
    status = ConditionStatus.PASS if count >= _MIN_REGISTERED_PRINCIPALS else ConditionStatus.FAIL
    return GateCondition(
        id="registered_human_principals",
        status=status,
        detail=f"{count} registered human principal(s) on file",
        metrics={"registered_principal_count": count},
    )


__all__ = [
    "ConditionStatus",
    "G4Inputs",
    "GateCondition",
    "GateReport",
    "evaluate_g4",
]
