"""Evidence collection and verification, without provider-specific imports."""

from __future__ import annotations

from .evaluator import (
    EvidenceContract,
    EvidenceEvaluation,
    EvidenceEvaluationError,
    EvidenceEvaluator,
    EvidenceRequirement,
)

__all__ = [
    "EvidenceContract",
    "EvidenceEvaluation",
    "EvidenceEvaluationError",
    "EvidenceEvaluator",
    "EvidenceRequirement",
]
