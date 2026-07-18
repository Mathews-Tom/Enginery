"""Evidence collection and verification, without provider-specific imports."""

from __future__ import annotations

from .evaluator import (
    EvidenceContract,
    EvidenceEvaluation,
    EvidenceEvaluationError,
    EvidenceEvaluator,
    EvidenceRequirement,
)
from .terminal import (
    MergeReadyContext,
    MergeReadyVerifier,
    ReleasedContext,
    ReleasedVerifier,
    TerminalContractError,
)

__all__ = [
    "EvidenceContract",
    "EvidenceEvaluation",
    "EvidenceEvaluationError",
    "EvidenceEvaluator",
    "EvidenceRequirement",
    "MergeReadyContext",
    "MergeReadyVerifier",
    "ReleasedContext",
    "ReleasedVerifier",
    "TerminalContractError",
]
