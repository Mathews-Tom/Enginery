from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.local import (
    LocalCapabilitySource,
    LocalDeploymentFixture,
    LocalGit,
    LocalPublication,
    LocalValidation,
    ScriptedHarness,
)
from enginery.application.adapter_types import AdapterEventKind, NormalizedAdapterEvent
from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    DeploymentRequest,
    PublicationRequest,
    ReleaseArtifact,
    ValidationRequest,
    ValidationStatus,
)
from enginery.application.work_ports import HarnessTask
from enginery.domain.digests import Digest
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult


def _artifact() -> ReleaseArtifact:
    return ReleaseArtifact(
        version="0.1.0",
        digest=Digest.of_bytes(b"distribution"),
        media_type="application/zip",
    )


def test_scripted_harness_streams_normalized_events_and_reconciles() -> None:
    event = NormalizedAdapterEvent(
        kind=AdapterEventKind.TERMINAL,
        occurred_at=datetime(2026, 7, 19, tzinfo=UTC),
        operation_id=OperationId("op-harness-1"),
        summary="worker completed",
    )
    harness = ScriptedHarness(emitted_events=(event,))
    task = HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId("op-harness-1"),
        workspace_path=Path("/tmp/workspace"),
        objective="Implement the task",
        acceptance_criteria=("tests pass",),
        constraints=("no network",),
        permitted_capabilities=("repository-read",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=None,
    )

    session = harness.start(task)

    assert harness.probe().fingerprint is not None
    assert tuple(harness.events(session)) == (event,)
    assert harness.result(session).terminal_status == "succeeded"
    assert harness.reconcile(operation_id=task.operation_id) is ReconciliationResult.FOUND_MATCHING
    assert (
        harness.reconcile(operation_id=OperationId("op-missing")) is ReconciliationResult.NOT_FOUND
    )


def test_local_validation_uses_argument_vectors_and_reconciles(tmp_path: Path) -> None:
    validator = LocalValidation()
    request = ValidationRequest(
        run_id=RunId("run-1"),
        workspace_path=tmp_path,
        revision="deadbeef",
        command=("python", "-c", "print('validated')"),
        operation_id=OperationId("op-validation-1"),
    )

    result = validator.validate(request)

    assert result.status is ValidationStatus.PASSED
    assert (
        validator.reconcile(operation_id=request.operation_id)
        is ReconciliationResult.FOUND_MATCHING
    )


def test_local_validation_redacts_output_before_digest(tmp_path: Path) -> None:
    validator = LocalValidation()
    request = ValidationRequest(
        run_id=RunId("run-1"),
        workspace_path=tmp_path,
        revision="deadbeef",
        command=("python", "-c", "print('AKIAABCDEFGHIJKLMNOP')"),
        operation_id=OperationId("op-validation-redaction"),
    )

    result = validator.validate(request)

    assert result.output_digest == Digest.of_bytes(b"[REDACTED:aws_access_key_id]\n")
    assert result.output_digest != Digest.of_bytes(b"AKIAABCDEFGHIJKLMNOP\n")


def test_local_validation_normalizes_missing_command(tmp_path: Path) -> None:
    validator = LocalValidation()
    request = ValidationRequest(
        run_id=RunId("run-1"),
        workspace_path=tmp_path,
        revision="deadbeef",
        command=("enginery-command-does-not-exist",),
        operation_id=OperationId("op-validation-missing"),
    )

    result = validator.validate(request)

    assert result.status is ValidationStatus.ERRORED
    assert result.exit_code == 127


def test_local_git_probe_reports_misconfigured_repository(tmp_path: Path) -> None:
    status = LocalGit(tmp_path).probe()

    assert status.availability.value == "misconfigured"
    assert status.fingerprint is None


def test_local_publication_requires_destination_verification(tmp_path: Path) -> None:
    publisher = LocalPublication(tmp_path)
    request = PublicationRequest(
        run_id=RunId("run-1"),
        artifact=_artifact(),
        destination="fixture-registry",
        operation_id=OperationId("op-publish-1"),
    )

    receipt = publisher.publish(request)

    assert publisher.probe().fingerprint is not None
    assert publisher.verify(receipt) == receipt
    assert (
        publisher.reconcile(operation_id=request.operation_id)
        is ReconciliationResult.FOUND_MATCHING
    )


def test_deployment_fixture_rolls_back_known_receipt() -> None:
    deployment = LocalDeploymentFixture()
    request = DeploymentRequest(
        run_id=RunId("run-1"),
        artifact=_artifact(),
        target="fixture-service",
        operation_id=OperationId("op-deploy-1"),
    )

    receipt = deployment.deploy(request)

    assert (
        deployment.rollback(receipt, operation_id=OperationId("op-rollback-1"))
        is ReconciliationResult.FOUND_MATCHING
    )
    assert (
        deployment.reconcile(operation_id=OperationId("op-rollback-1"))
        is ReconciliationResult.FOUND_MATCHING
    )


def test_local_capability_source_has_no_runtime_discovery() -> None:
    capability = CapabilityDescriptor(
        name="repository-read",
        version="1",
        digest=Digest.of_bytes(b"repository-read"),
        provenance="repository-local",
    )
    source = LocalCapabilitySource(capabilities=(capability,))

    assert source.discover() == (capability,)
    assert source.resolve("repository-read", "1") == capability
    assert source.resolve("missing", "1") is None
