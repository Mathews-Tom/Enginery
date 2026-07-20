"""Capability-specific exceptions mapped onto the domain failure taxonomy.

Every exception here extends an existing :class:`enginery.domain.errors.EngineryError`
subclass so the CLI exit-code contract and adapter failure classification
extend to capability resolution, provenance, and materialization without a
second taxonomy.
"""

from __future__ import annotations

from enginery.domain.errors import (
    HumanActionRequiredError,
    MissingPrerequisiteError,
    ValidationFailureError,
)


class CapabilityNotFoundError(MissingPrerequisiteError):
    """Raised when a requested capability is not offered by any configured source."""


class CapabilityDigestMismatchError(ValidationFailureError):
    """Raised when bytes fetched for a capability do not hash to its resolved digest.

    This is the digest-swap defense: a source that reports one digest but
    serves different bytes is rejected before the bytes ever reach a lock
    or a workspace.
    """


class CapabilityLockDriftError(MissingPrerequisiteError):
    """Raised when a mutable capability source resolves an already-locked
    name/version to a different digest than an in-flight lock recorded.

    Mirrors :func:`enginery.application.adapter_types.require_matching_fingerprints`:
    a changed reference blocks resumption rather than silently migrating.
    """


class CapabilityIntegrityError(ValidationFailureError):
    """Raised when supplied or previously materialized bytes no longer
    match the digest a lock entry recorded."""


class CapabilityApprovalRequiredError(HumanActionRequiredError):
    """Raised when materialization is attempted for a capability that still
    needs interactive, exact-digest human approval before it can execute."""


class CapabilityLicenseMismatchError(ValidationFailureError):
    """Raised when a resolved capability's license does not match what the
    caller declared it expected, so an unexpected license never silently
    enters a lock."""


__all__ = [
    "CapabilityApprovalRequiredError",
    "CapabilityDigestMismatchError",
    "CapabilityIntegrityError",
    "CapabilityLicenseMismatchError",
    "CapabilityLockDriftError",
    "CapabilityNotFoundError",
]
