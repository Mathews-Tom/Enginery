"""Canonical declared GitHub work classification vocabulary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from enginery.application.work_ports import DeclaredWorkClassification
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError, TransientProviderFailureError

WORK_KIND_LABELS = {
    "enginery/work-kind/issue": WorkKind.ISSUE,
    "enginery/work-kind/plan": WorkKind.PLAN,
}
RISK_LABELS = {
    "enginery/risk/low": RiskClass.LOW,
    "enginery/risk/medium": RiskClass.MEDIUM,
}
CLASSIFICATION_PREFIXES = ("enginery/work-kind/", "enginery/risk/")


@dataclass(frozen=True, slots=True)
class IssueCandidate:
    external_reference: str
    title: str
    labels: tuple[str, ...]
    updated_at: str


def label_names(labels: Sequence[object]) -> tuple[str, ...]:
    """Normalize API labels without losing repeated classification labels."""
    names: list[str] = []
    for label in labels:
        if not isinstance(label, Mapping):
            raise TransientProviderFailureError("GitHub issue label record must be an object")
        name = label.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidInputError("GitHub response field 'name' must be a non-blank string")
        names.append(name)
    return tuple(sorted(names))


def declared_classification(
    label_names: Sequence[str], *, source_url: str = "github-issues"
) -> tuple[WorkKind, RiskClass, DeclaredWorkClassification]:
    work_labels = [
        name for name in label_names if name.casefold().startswith(CLASSIFICATION_PREFIXES[0])
    ]
    risk_labels = [
        name for name in label_names if name.casefold().startswith(CLASSIFICATION_PREFIXES[1])
    ]
    if len(work_labels) != 1:
        raise InvalidInputError("GitHub issue requires exactly one declared work-kind label")
    if len(risk_labels) != 1:
        raise InvalidInputError("GitHub issue requires exactly one declared risk label")
    if work_labels[0] not in WORK_KIND_LABELS:
        raise InvalidInputError("GitHub work-kind labels must use the exact declared vocabulary")
    if risk_labels[0] not in RISK_LABELS:
        raise InvalidInputError("GitHub risk labels must use the exact declared vocabulary")
    work_label, risk_label = work_labels[0], risk_labels[0]
    return (
        WORK_KIND_LABELS[work_label],
        RISK_LABELS[risk_label],
        DeclaredWorkClassification(
            source_provider="github-issues",
            source_url=source_url,
            canonical_labels=(work_label, risk_label),
        ),
    )
