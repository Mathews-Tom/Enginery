from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    DeploymentReceipt,
    DeploymentRequest,
    PublicationReceipt,
    PublicationRequest,
    ReleaseArtifact,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
)
from enginery.domain.digests import Digest
from enginery.domain.ids import OperationId, RunId

_ARTIFACT = ReleaseArtifact(
    version="0.1.0",
    digest=Digest.of_bytes(b"distribution"),
    media_type="application/zip",
)


def test_validation_request_rejects_relative_workspace() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ValidationRequest(
            run_id=RunId("run-1"),
            workspace_path=Path("workspace"),
            revision="deadbeef",
            command=("uv", "run", "pytest"),
            operation_id=OperationId("op-validation-1"),
        )


@pytest.mark.parametrize(
    ("revision", "command"),
    [
        (" ", ("uv",)),
        ("deadbeef", ()),
        ("deadbeef", ("uv", " ")),
    ],
)
def test_validation_request_rejects_invalid_subject(
    revision: str, command: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        ValidationRequest(
            run_id=RunId("run-1"),
            workspace_path=Path("/tmp/workspace"),
            revision=revision,
            command=command,
            operation_id=OperationId("op-validation-1"),
        )


def test_passing_validation_requires_zero_exit_code() -> None:
    with pytest.raises(ValueError, match="exit code zero"):
        ValidationResult(
            revision="deadbeef",
            status=ValidationStatus.PASSED,
            exit_code=1,
            output_digest=Digest.of_bytes(b"output"),
        )


def test_validation_result_rejects_blank_revision() -> None:
    with pytest.raises(ValueError, match="revision"):
        ValidationResult(
            revision=" ",
            status=ValidationStatus.FAILED,
            exit_code=1,
            output_digest=Digest.of_bytes(b"output"),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ReleaseArtifact(
            version=" ", digest=Digest.of_bytes(b"distribution"), media_type="application/zip"
        ),
        lambda: ReleaseArtifact(
            version="0.1.0", digest=Digest.of_bytes(b"distribution"), media_type=" "
        ),
        lambda: PublicationRequest(
            run_id=RunId("run-1"),
            artifact=_ARTIFACT,
            destination=" ",
            operation_id=OperationId("op-publish-1"),
        ),
        lambda: PublicationReceipt(
            destination=" ", version="0.1.0", artifact_digest=_ARTIFACT.digest
        ),
        lambda: PublicationReceipt(
            destination="fixture", version=" ", artifact_digest=_ARTIFACT.digest
        ),
        lambda: DeploymentRequest(
            run_id=RunId("run-1"),
            artifact=_ARTIFACT,
            target=" ",
            operation_id=OperationId("op-deploy-1"),
        ),
        lambda: DeploymentReceipt(
            target="fixture", artifact_digest=_ARTIFACT.digest, deployment_id=" "
        ),
        lambda: DeploymentReceipt(
            target=" ", artifact_digest=_ARTIFACT.digest, deployment_id="deployment-1"
        ),
        lambda: CapabilityDescriptor(
            name=" ", version="1", digest=Digest.of_bytes(b"capability"), provenance="local"
        ),
        lambda: CapabilityDescriptor(
            name="capability",
            version=" ",
            digest=Digest.of_bytes(b"capability"),
            provenance="local",
        ),
        lambda: CapabilityDescriptor(
            name="capability", version="1", digest=Digest.of_bytes(b"capability"), provenance=" "
        ),
    ],
)
def test_delivery_values_reject_blank_security_boundaries(factory: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        factory()
