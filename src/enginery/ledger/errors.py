"""Ledger-specific exceptions.

Every exception here maps to an existing :class:`enginery.domain.errors.FailureClass`
so the CLI exit-code contract and adapter failure taxonomy from the domain layer
extend to storage failures without inventing a second taxonomy.
"""

from __future__ import annotations

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    MissingPrerequisiteError,
    ValidationFailureError,
)


class ExpectedVersionConflictError(ExternalConflictError):
    """Raised when a command's expected aggregate, process-manager, or lease
    version no longer matches the ledger's current version. The whole
    command transaction is rolled back; nothing partially commits."""


class MigrationFailedError(InternalInvariantViolationError):
    """Raised when a forward migration fails to apply. The failing
    migration's transaction is rolled back and no later migration runs, so
    the ledger stays pinned at its last known-good schema version."""


class SchemaVersionUnsupportedError(InternalInvariantViolationError):
    """Raised when the ledger file records a schema version this build of
    Enginery does not recognize as a valid migration target."""


class CorruptedEventError(InternalInvariantViolationError):
    """Raised when a stored event row fails to decode as valid, schema-known
    payload data during read or replay."""


class ArtifactMissingError(MissingPrerequisiteError):
    """Raised when artifact metadata references a digest with no
    corresponding content-addressed bytes on disk."""


class ArtifactDigestMismatchError(ValidationFailureError):
    """Raised when stored artifact bytes no longer hash to the digest
    recorded in their metadata."""


class RawCredentialDetectedError(ValidationFailureError):
    """Raised when a write path observes credential-shaped content headed
    for the ledger or artifact store. This is a heuristic backstop, not an
    absolute guarantee of detecting every secret format."""


__all__ = [
    "ArtifactDigestMismatchError",
    "ArtifactMissingError",
    "CorruptedEventError",
    "ExpectedVersionConflictError",
    "MigrationFailedError",
    "RawCredentialDetectedError",
    "SchemaVersionUnsupportedError",
]
