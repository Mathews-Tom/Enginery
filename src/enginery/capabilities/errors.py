"""Capability-specific exceptions mapped onto the domain failure taxonomy.

Every exception here extends an existing :class:`enginery.domain.errors.EngineryError`
subclass so the CLI exit-code contract and adapter failure classification
extend to capability resolution, provenance, and materialization without a
second taxonomy. Every capability failure is also a
:class:`CapabilityError`, so a caller can catch the whole family.
"""

from __future__ import annotations

from enginery.domain.errors import (
    EngineryError,
    HumanActionRequiredError,
    MissingPrerequisiteError,
    ValidationFailureError,
)


class CapabilityError(EngineryError):
    """Marker base for every failure raised by the capability subsystem."""


class CapabilityNotFoundError(CapabilityError, MissingPrerequisiteError):
    """Raised when a requested capability is not offered by any configured source."""


class CapabilityDigestMismatchError(CapabilityError, ValidationFailureError):
    """Raised when files fetched for a capability do not hash to its resolved closure digest.

    This is the digest-swap defense: a source that reports one closure
    digest but serves different files is rejected before any file reaches a
    lock or a workspace.
    """


class CapabilityLockDriftError(CapabilityError, MissingPrerequisiteError):
    """Raised when a mutable capability source resolves an already-locked
    name/version to a different closure digest than an in-flight lock recorded.

    Mirrors :func:`enginery.application.adapter_types.require_matching_fingerprints`:
    a changed reference blocks resumption rather than silently migrating.
    """


class CapabilityIntegrityError(CapabilityError, ValidationFailureError):
    """Raised when supplied or previously materialized files no longer
    match the closure a lock entry recorded."""


class CapabilityApprovalRequiredError(CapabilityError, HumanActionRequiredError):
    """Raised when materialization is attempted for a capability that still
    needs interactive, exact-digest human approval before it can execute."""


class CapabilityLicenseMismatchError(CapabilityError, ValidationFailureError):
    """Raised when a resolved capability's license does not match what the
    caller declared it expected, so an unexpected license never silently
    enters a lock."""


class CapabilityLicenseHoldError(CapabilityError, ValidationFailureError):
    """Raised when a package's license is unknown, absent, or not on the
    reviewed redistributable list, so it fails closed for redistribution."""


class CapabilityPathError(CapabilityError, ValidationFailureError):
    """Raised for an absolute, traversing, duplicate, case-colliding, or
    otherwise unsafe package-relative path."""


class CapabilitySymlinkError(CapabilityError, ValidationFailureError):
    """Raised for a symlink, hardlinked file, or non-regular file in a package."""


class CapabilityAssetTypeError(CapabilityError, ValidationFailureError):
    """Raised for an asset whose type is not on the reviewed list, or that
    carries an executable bit without being a script."""


class CapabilityClosureLimitError(CapabilityError, ValidationFailureError):
    """Raised when a package exceeds the file-count, file-size, or total-size limit."""


class CapabilityManifestError(CapabilityError, ValidationFailureError):
    """Raised when a package manifest is missing, malformed, or disagrees
    with the identity the package was requested under."""


class CapabilityDependencyError(CapabilityError, ValidationFailureError):
    """Raised when a declared dependency is unresolvable, cyclic, conflicting,
    or resolves to a closure other than the digest its pin declares."""


class CapabilityLegacyLockError(CapabilityError, ValidationFailureError):
    """Raised when a legacy single-blob lock is used where a closure lock is
    required; it must be re-resolved, never silently upgraded."""


__all__ = [
    "CapabilityApprovalRequiredError",
    "CapabilityAssetTypeError",
    "CapabilityClosureLimitError",
    "CapabilityDependencyError",
    "CapabilityDigestMismatchError",
    "CapabilityError",
    "CapabilityIntegrityError",
    "CapabilityLegacyLockError",
    "CapabilityLicenseHoldError",
    "CapabilityLicenseMismatchError",
    "CapabilityLockDriftError",
    "CapabilityManifestError",
    "CapabilityNotFoundError",
    "CapabilityPathError",
    "CapabilitySymlinkError",
]
