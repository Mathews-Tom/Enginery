"""CLI exit-code contract.

Maps every ``FailureClass`` to a stable, distinct process exit code so
scripts driving the CLI can distinguish success, policy denial, human
action required, blocked prerequisites, external conflict, cancellation,
and internal failure without parsing stderr text.
"""

from __future__ import annotations

from enginery.domain.errors import FailureClass

SUCCESS = 0

_EXIT_CODES: dict[FailureClass, int] = {
    FailureClass.INTERNAL_INVARIANT_VIOLATION: 1,
    FailureClass.INVALID_INPUT: 2,
    FailureClass.MISSING_PREREQUISITE: 3,
    FailureClass.POLICY_DENIAL: 4,
    FailureClass.HUMAN_ACTION_REQUIRED: 5,
    FailureClass.TRANSIENT_PROVIDER_FAILURE: 6,
    FailureClass.AUTHENTICATION_FAILURE: 7,
    FailureClass.RATE_LIMIT: 8,
    FailureClass.EXTERNAL_CONFLICT: 9,
    FailureClass.STALE_EVIDENCE: 10,
    FailureClass.WORKER_FAILURE: 11,
    FailureClass.VALIDATION_FAILURE: 12,
    FailureClass.TIMEOUT: 13,
    FailureClass.CANCELLATION: 14,
    FailureClass.AMBIGUOUS_EXTERNAL_SIDE_EFFECT: 15,
}

if set(_EXIT_CODES) != set(FailureClass):
    raise AssertionError("every FailureClass must map to an exit code")


def exit_code_for(failure_class: FailureClass) -> int:
    return _EXIT_CODES[failure_class]
