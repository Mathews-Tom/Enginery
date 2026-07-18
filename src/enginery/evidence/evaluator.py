"""Deterministic pass, fail, and indeterminate evidence evaluation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from enginery.domain.errors import InvalidInputError
from enginery.domain.evidence import EvidenceItem
from enginery.domain.node_attempt import EvidenceResult


class EvidenceEvaluationError(InvalidInputError):
    """Raised when an evidence contract is structurally invalid."""


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """A named evidence requirement with an optional exact current subject."""

    key: str
    evidence_type: str
    subject_revision: str | None = None
    subject_resource: str | None = None
    hard_required: bool = True

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.evidence_type.strip():
            raise EvidenceEvaluationError("evidence requirement key and type must be non-blank")


@dataclass(frozen=True, slots=True)
class EvidenceContract:
    """The complete contract evaluated before a workflow claim can advance."""

    requirements: tuple[EvidenceRequirement, ...]

    def __post_init__(self) -> None:
        keys = [requirement.key for requirement in self.requirements]
        if len(set(keys)) != len(keys):
            raise EvidenceEvaluationError("evidence requirement keys must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceEvaluation:
    """Auditable evaluation outcome; only ``PASS`` supports success."""

    result: EvidenceResult
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    indeterminate: tuple[str, ...]
    stale: tuple[str, ...]
    subject_mismatch: tuple[str, ...]

    @property
    def blocks_terminal(self) -> bool:
        return self.result is not EvidenceResult.PASS


class EvidenceEvaluator:
    """Evaluate evidence without converting uncertainty into a pass."""

    def evaluate(
        self,
        contract: EvidenceContract,
        evidence_items: Iterable[EvidenceItem],
        reference_time: datetime,
    ) -> EvidenceEvaluation:
        """Return a classified result for every required evidence item."""

        evidence_by_type: dict[str, list[EvidenceItem]] = {}
        for item in evidence_items:
            evidence_by_type.setdefault(item.type, []).append(item)
        satisfied: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        indeterminate: list[str] = []
        stale: list[str] = []
        subject_mismatch: list[str] = []
        for requirement in contract.requirements:
            candidates = evidence_by_type.get(requirement.evidence_type, [])
            matching = [
                item
                for item in candidates
                if item.binds_subject(requirement.subject_revision, requirement.subject_resource)
            ]
            if not matching:
                if candidates:
                    subject_mismatch.append(requirement.key)
                elif requirement.hard_required:
                    missing.append(requirement.key)
                continue
            latest = max(matching, key=lambda item: item.observed_time)
            if latest.is_stale(reference_time):
                stale.append(requirement.key)
            elif latest.result is EvidenceResult.FAIL:
                failed.append(requirement.key)
            elif latest.result is EvidenceResult.INDETERMINATE:
                indeterminate.append(requirement.key)
            else:
                satisfied.append(requirement.key)
        result = (
            EvidenceResult.FAIL
            if failed or stale or subject_mismatch
            else EvidenceResult.INDETERMINATE
            if missing or indeterminate
            else EvidenceResult.PASS
        )
        return EvidenceEvaluation(
            result=result,
            satisfied=tuple(satisfied),
            missing=tuple(missing),
            failed=tuple(failed),
            indeterminate=tuple(indeterminate),
            stale=tuple(stale),
            subject_mismatch=tuple(subject_mismatch),
        )


__all__ = [
    "EvidenceContract",
    "EvidenceEvaluation",
    "EvidenceEvaluationError",
    "EvidenceEvaluator",
    "EvidenceRequirement",
]
