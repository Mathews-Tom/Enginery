"""Provider-neutral ports for validation, publication, deployment, and capabilities."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from enginery.application.adapter_types import AdapterStatus
from enginery.domain.digests import Digest
from enginery.domain.ids import OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult


class ValidationStatus(enum.StrEnum):
    """The normalized terminal status of deterministic validation."""

    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    """One exact-revision deterministic validation invocation."""

    run_id: RunId
    workspace_path: Path
    revision: str
    command: tuple[str, ...]
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not self.workspace_path.is_absolute():
            raise ValueError("validation workspace_path must be absolute")
        if not self.revision.strip():
            raise ValueError("validation revision must be non-blank")
        if not self.command or any(not argument.strip() for argument in self.command):
            raise ValueError("validation command must contain non-blank arguments")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Artifact-backed validation evidence bound to a specific revision."""

    revision: str
    status: ValidationStatus
    exit_code: int
    output_digest: Digest

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValueError("validation result revision must be non-blank")
        if self.status is ValidationStatus.PASSED and self.exit_code != 0:
            raise ValueError("passing validation must have exit code zero")


class ValidationPort(Protocol):
    """Execute local or hosted checks while preserving exact-subject evidence."""

    def probe(self) -> AdapterStatus: ...

    def validate(self, request: ValidationRequest) -> ValidationResult: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    """An immutable release candidate eligible for fixed-broker publication."""

    version: str
    digest: Digest
    media_type: str

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.media_type.strip():
            raise ValueError("release artifact version and media_type must be non-blank")


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """A fixed-target publication request bound to an approved artifact."""

    run_id: RunId
    artifact: ReleaseArtifact
    destination: str
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not self.destination.strip():
            raise ValueError("publication destination must be non-blank")


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Destination evidence returned after a publication attempt."""

    destination: str
    version: str
    artifact_digest: Digest

    def __post_init__(self) -> None:
        if not self.destination.strip() or not self.version.strip():
            raise ValueError("publication receipt destination and version must be non-blank")


class ReleasePort(Protocol):
    """Publish and verify fixed artifacts without exposing broker credentials."""

    def probe(self) -> AdapterStatus: ...

    def publish(self, request: PublicationRequest) -> PublicationReceipt: ...

    def verify(self, receipt: PublicationReceipt) -> PublicationReceipt: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    """A fixed-service deployment request bound to an immutable artifact."""

    run_id: RunId
    artifact: ReleaseArtifact
    target: str
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise ValueError("deployment target must be non-blank")


@dataclass(frozen=True, slots=True)
class DeploymentReceipt:
    """Observed deployment identity for a controlled target."""

    target: str
    artifact_digest: Digest
    deployment_id: str

    def __post_init__(self) -> None:
        if not self.target.strip() or not self.deployment_id.strip():
            raise ValueError("deployment target and deployment_id must be non-blank")


class DeploymentPort(Protocol):
    """Deploy, observe, and roll back controlled targets through fixed APIs."""

    def probe(self) -> AdapterStatus: ...

    def deploy(self, request: DeploymentRequest) -> DeploymentReceipt: ...

    def rollback(
        self, receipt: DeploymentReceipt, *, operation_id: OperationId
    ) -> ReconciliationResult: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """A content-addressed capability exposed by a configured source."""

    name: str
    version: str
    digest: Digest
    provenance: str
    license: str | None = None

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.name, self.version, self.provenance)):
            raise ValueError("capability name, version, and provenance must be non-blank")
        if self.license is not None and not self.license.strip():
            raise ValueError("capability license, when present, must be non-blank")


class CapabilitySourcePort(Protocol):
    """Discover repository-local capability metadata without runtime plugins."""

    def probe(self) -> AdapterStatus: ...

    def discover(self) -> tuple[CapabilityDescriptor, ...]: ...

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None: ...

    def fetch(self, name: str, version: str) -> bytes: ...


__all__ = [
    "CapabilityDescriptor",
    "CapabilitySourcePort",
    "DeploymentPort",
    "DeploymentReceipt",
    "DeploymentRequest",
    "PublicationReceipt",
    "PublicationRequest",
    "ReleaseArtifact",
    "ReleasePort",
    "ValidationPort",
    "ValidationRequest",
    "ValidationResult",
    "ValidationStatus",
]
