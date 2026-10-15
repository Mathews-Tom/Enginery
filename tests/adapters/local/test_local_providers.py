from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.capabilities import descriptor_for, package_files
from tests.support.execution import selection_for

from enginery.adapters.local import (
    LocalCapabilitySource,
    LocalDeploymentFixture,
    LocalGit,
    LocalPublication,
    LocalValidation,
    LocalWorkspace,
    ScriptedHarness,
)
from enginery.application.adapter_types import AdapterEventKind, NormalizedAdapterEvent
from enginery.application.delivery_ports import (
    CapabilityListing,
    DeploymentRequest,
    PublicationRequest,
    ReleaseArtifact,
    ValidationRequest,
    ValidationStatus,
)
from enginery.application.work_ports import HarnessTask, WorkspaceHandle, WorkspaceRequest
from enginery.domain.digests import Digest
from enginery.domain.errors import MissingPrerequisiteError
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
        selection=selection_for("omp"),
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


def _repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=path, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (path / "README").write_text("fixture\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-q", "-m", "fixture")
    return path, git("rev-parse", "HEAD")


def test_a_local_workspace_carries_no_environment_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base = _repository(tmp_path / "repository")
    monkeypatch.setenv("GH_TOKEN", "must-never-be-copied")
    workspaces = LocalWorkspace(tmp_path / "workspaces")

    handle = workspaces.create(
        WorkspaceRequest(
            run_id=RunId("run-1"),
            repository_id="org/repo",
            repository_path=repository,
            base_revision=base,
            operation_id=OperationId("op-workspace-env"),
        )
    )

    assert handle.path.is_dir()
    assert "GH_TOKEN" not in repr(handle)
    workspaces.cleanup(handle, operation_id=OperationId("op-workspace-env-cleanup"))


def test_the_launch_environment_has_one_allowlist_and_a_workspace_request_has_no_second_one() -> (
    None
):
    request_fields = {field.name for field in dataclasses.fields(WorkspaceRequest)}
    handle_fields = {field.name for field in dataclasses.fields(WorkspaceHandle)}

    assert "permitted_environment_keys" not in request_fields
    assert "environment" not in handle_fields


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
    files = package_files("repository-read")
    capability = descriptor_for(files)
    source = LocalCapabilitySource(capabilities=(capability,))

    assert source.discover() == (
        CapabilityListing(
            name=capability.name,
            version=capability.version,
            provenance=capability.provenance,
            revision=capability.revision,
        ),
    )
    assert source.resolve("repository-read", "1") == capability
    assert source.resolve("missing", "1") is None


def test_local_capability_source_fetches_the_registered_package_closure() -> None:
    files = package_files("repository-read", extra={"scripts/run.py": b"print(1)\n"})
    capability = descriptor_for(files)
    source = LocalCapabilitySource(
        capabilities=(capability,), packages={("repository-read", "1"): files}
    )

    assert source.fetch_closure("repository-read", "1") == files
    with pytest.raises(MissingPrerequisiteError):
        source.fetch_closure("repository-read", "2")
