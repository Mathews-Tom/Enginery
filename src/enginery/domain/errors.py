"""Central failure-class taxonomy and exception hierarchy.

Every exception raised across Enginery's layers carries a stable
``FailureClass`` (03_SYSTEM_DESIGN.md §24) so callers such as the CLI exit-
code mapping, retry policy, and evidence recording can react to failures
without parsing exception messages. This module has zero outward imports:
every other layer, including adapters and the CLI, may import it.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import ClassVar


class FailureClass(enum.Enum):
    """The fifteen failure classes required by the system design."""

    INVALID_INPUT = "invalid_input"
    MISSING_PREREQUISITE = "missing_prerequisite"
    POLICY_DENIAL = "policy_denial"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    TRANSIENT_PROVIDER_FAILURE = "transient_provider_failure"
    AUTHENTICATION_FAILURE = "authentication_failure"
    RATE_LIMIT = "rate_limit"
    EXTERNAL_CONFLICT = "external_conflict"
    STALE_EVIDENCE = "stale_evidence"
    WORKER_FAILURE = "worker_failure"
    VALIDATION_FAILURE = "validation_failure"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    AMBIGUOUS_EXTERNAL_SIDE_EFFECT = "ambiguous_external_side_effect"
    INTERNAL_INVARIANT_VIOLATION = "internal_invariant_violation"


class EngineryError(Exception):
    """Base class for every exception raised by Enginery code.

    Subclasses declare a fixed ``failure_class`` used by callers to react to
    a failure category without string matching.
    """

    failure_class: ClassVar[FailureClass]

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: Mapping[str, object] = details or {}


class InvalidInputError(EngineryError):
    failure_class = FailureClass.INVALID_INPUT


class MissingPrerequisiteError(EngineryError):
    failure_class = FailureClass.MISSING_PREREQUISITE


class PolicyDenialError(EngineryError):
    failure_class = FailureClass.POLICY_DENIAL


class HumanActionRequiredError(EngineryError):
    failure_class = FailureClass.HUMAN_ACTION_REQUIRED


class TransientProviderFailureError(EngineryError):
    failure_class = FailureClass.TRANSIENT_PROVIDER_FAILURE


class AuthenticationFailureError(EngineryError):
    failure_class = FailureClass.AUTHENTICATION_FAILURE


class RateLimitError(EngineryError):
    failure_class = FailureClass.RATE_LIMIT


class ExternalConflictError(EngineryError):
    failure_class = FailureClass.EXTERNAL_CONFLICT


class StaleEvidenceError(EngineryError):
    failure_class = FailureClass.STALE_EVIDENCE


class WorkerFailureError(EngineryError):
    failure_class = FailureClass.WORKER_FAILURE


class ValidationFailureError(EngineryError):
    failure_class = FailureClass.VALIDATION_FAILURE


class OperationTimeoutError(EngineryError):
    failure_class = FailureClass.TIMEOUT


class CancellationError(EngineryError):
    failure_class = FailureClass.CANCELLATION


class AmbiguousExternalSideEffectError(EngineryError):
    failure_class = FailureClass.AMBIGUOUS_EXTERNAL_SIDE_EFFECT


class InternalInvariantViolationError(EngineryError):
    failure_class = FailureClass.INTERNAL_INVARIANT_VIOLATION


__all__ = [
    "AmbiguousExternalSideEffectError",
    "AuthenticationFailureError",
    "CancellationError",
    "EngineryError",
    "ExternalConflictError",
    "FailureClass",
    "HumanActionRequiredError",
    "InternalInvariantViolationError",
    "InvalidInputError",
    "MissingPrerequisiteError",
    "OperationTimeoutError",
    "PolicyDenialError",
    "RateLimitError",
    "StaleEvidenceError",
    "TransientProviderFailureError",
    "ValidationFailureError",
    "WorkerFailureError",
]
