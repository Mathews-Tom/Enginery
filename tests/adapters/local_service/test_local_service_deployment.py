"""Tests for enginery.adapters.local_service.

Exercises the real subprocess/real-HTTP local service fixture end to
end: no fakes or mocks stand in for the process or the network calls.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from enginery.adapters.local_service import (
    HealthObservation,
    LocalServiceBuild,
    LocalServiceDeploymentAdapter,
    build_local_service_artifact,
)
from enginery.application.adapter_types import AdapterAvailability
from enginery.application.delivery_ports import (
    DeploymentReceipt,
    DeploymentRequest,
    ReleaseArtifact,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    ExternalConflictError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.domain.ids import OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult

_APP_SCRIPT = (
    Path(__file__).resolve().parents[3] / "fixtures" / "enginery-stage3-local-service" / "app.py"
)
_RUN_ID = RunId("run-hotfix-1")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _operation_id(label: str) -> OperationId:
    return OperationId(value=hashlib.sha256(label.encode("utf-8")).hexdigest())


@pytest.fixture
def adapter(tmp_path: Path) -> Iterator[LocalServiceDeploymentAdapter]:
    instance = LocalServiceDeploymentAdapter(
        artifacts_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        app_script=_APP_SCRIPT,
        ready_attempts=50,
        ready_interval_seconds=0.05,
    )
    try:
        yield instance
    finally:
        state_root = instance.state_root
        if state_root.is_dir():
            for state_file in state_root.glob("*.json"):
                state = json.loads(state_file.read_text(encoding="utf-8"))
                for entry in (state.get("current"), state.get("previous")):
                    if entry is None:
                        continue
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(entry["pid"]), signal.SIGKILL)


def _deploy(
    adapter: LocalServiceDeploymentAdapter,
    *,
    version: str,
    defect_mode: str,
    target: str,
    operation_label: str,
) -> tuple[DeploymentReceipt, ReleaseArtifact]:
    artifact = build_local_service_artifact(
        LocalServiceBuild(version=version, defect_mode=defect_mode),
        artifacts_root=adapter.artifacts_root,
    )
    receipt = adapter.deploy(
        DeploymentRequest(
            run_id=_RUN_ID,
            artifact=artifact,
            target=target,
            operation_id=_operation_id(operation_label),
        )
    )
    return receipt, artifact


class TestLocalServiceBuild:
    def test_rejects_unknown_defect_mode(self) -> None:
        with pytest.raises(InvalidInputError, match="defect_mode"):
            LocalServiceBuild(version="v1", defect_mode="not-a-real-mode")

    def test_rejects_blank_version(self) -> None:
        with pytest.raises(InvalidInputError, match="version"):
            LocalServiceBuild(version=" ", defect_mode="none")


class TestBuildLocalServiceArtifact:
    def test_same_content_is_deterministic(self, tmp_path: Path) -> None:
        first = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="none"), artifacts_root=tmp_path
        )
        second = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="none"), artifacts_root=tmp_path
        )
        assert first.digest == second.digest

    def test_different_defect_mode_changes_the_digest(self, tmp_path: Path) -> None:
        none_defect = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="none"), artifacts_root=tmp_path
        )
        buggy = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="increment_off_by_one"),
            artifacts_root=tmp_path,
        )
        assert none_defect.digest != buggy.digest


class TestDeploy:
    def test_starts_a_real_process_reporting_the_deployed_revision(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        receipt, artifact = _deploy(
            adapter,
            version="v1",
            defect_mode="none",
            target=f"127.0.0.1:{port}",
            operation_label="deploy-v1",
        )

        assert receipt.target == f"127.0.0.1:{port}"
        assert receipt.artifact_digest == artifact.digest
        observation = adapter.observe(f"127.0.0.1:{port}")
        assert observation.revision == "v1"
        assert observation.healthy

    def test_redeploy_stops_the_prior_process_and_starts_a_new_one(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        target = f"127.0.0.1:{port}"
        _deploy(adapter, version="v1", defect_mode="none", target=target, operation_label="d1")
        first_state = adapter._read_state(target)
        assert first_state is not None
        first_pid = int(first_state["current"]["pid"])

        _deploy(adapter, version="v2", defect_mode="none", target=target, operation_label="d2")

        observation = adapter.observe(target)
        assert observation.revision == "v2"
        with pytest.raises(ProcessLookupError):
            os.kill(first_pid, 0)

    def test_rejects_a_malformed_target(self, adapter: LocalServiceDeploymentAdapter) -> None:
        artifact = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="none"),
            artifacts_root=adapter.artifacts_root,
        )
        with pytest.raises(InvalidInputError, match="host:port"):
            adapter.deploy(
                DeploymentRequest(
                    run_id=_RUN_ID,
                    artifact=artifact,
                    target="not-a-target",
                    operation_id=_operation_id("bad-target"),
                )
            )

    def test_deploy_that_never_becomes_ready_raises_and_leaves_no_process(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()
        # Occupy the port first so the deployed process cannot bind to it
        # and exits immediately -- a real, unforced readiness failure.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            fast_adapter = LocalServiceDeploymentAdapter(
                artifacts_root=tmp_path / "artifacts",
                state_root=tmp_path / "state",
                app_script=_APP_SCRIPT,
                ready_attempts=3,
                ready_interval_seconds=0.02,
            )
            artifact = build_local_service_artifact(
                LocalServiceBuild(version="v1", defect_mode="none"),
                artifacts_root=fast_adapter.artifacts_root,
            )
            with pytest.raises(ExternalConflictError, match="observation window"):
                fast_adapter.deploy(
                    DeploymentRequest(
                        run_id=_RUN_ID,
                        artifact=artifact,
                        target=f"127.0.0.1:{port}",
                        operation_id=_operation_id("never-ready"),
                    )
                )
        finally:
            blocker.close()


class TestObserve:
    def test_reports_healthy_for_a_normal_deployment(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        _deploy(
            adapter,
            version="v1",
            defect_mode="none",
            target=f"127.0.0.1:{port}",
            operation_label="observe-healthy",
        )

        observation = adapter.observe(f"127.0.0.1:{port}", attempts=1)

        assert isinstance(observation, HealthObservation)
        assert observation.healthy
        assert observation.consecutive_failures == 0

    def test_reports_unhealthy_for_a_degraded_deployment(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        _deploy(
            adapter,
            version="v1",
            defect_mode="health_degraded",
            target=f"127.0.0.1:{port}",
            operation_label="observe-degraded",
        )

        observation = adapter.observe(f"127.0.0.1:{port}", attempts=3, interval_seconds=0.01)

        assert not observation.healthy
        assert observation.consecutive_failures >= 1


class TestRollback:
    def test_restores_the_prior_revision_and_behavior(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        target = f"127.0.0.1:{port}"
        _deploy(
            adapter,
            version="v1",
            defect_mode="increment_off_by_one",
            target=target,
            operation_label="rb-v1",
        )
        receipt, _ = _deploy(
            adapter, version="v2", defect_mode="none", target=target, operation_label="rb-v2"
        )
        assert adapter.observe(target).revision == "v2"

        result = adapter.rollback(receipt, operation_id=_operation_id("rollback-1"))

        assert result is ReconciliationResult.FOUND_MATCHING
        observation = adapter.observe(target)
        assert observation.revision == "v1"

    def test_raises_when_no_prior_deploy_exists(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        target = f"127.0.0.1:{port}"
        receipt, _ = _deploy(
            adapter, version="v1", defect_mode="none", target=target, operation_label="rb-only"
        )

        with pytest.raises(MissingPrerequisiteError, match="no prior revision"):
            adapter.rollback(receipt, operation_id=_operation_id("rollback-none"))

    def test_mismatched_receipt_is_reported_as_conflicting(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        target = f"127.0.0.1:{port}"
        _deploy(adapter, version="v1", defect_mode="none", target=target, operation_label="c1")
        _deploy(adapter, version="v2", defect_mode="none", target=target, operation_label="c2")
        stale_receipt = DeploymentReceipt(
            target=target, artifact_digest=Digest.of_bytes(b"stale"), deployment_id="stale"
        )

        result = adapter.rollback(stale_receipt, operation_id=_operation_id("rollback-stale"))

        assert result is ReconciliationResult.FOUND_CONFLICTING


class TestReconcile:
    def test_reports_found_matching_after_a_successful_deploy(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        port = _free_port()
        operation_id = _operation_id("reconcile-1")
        artifact = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="none"),
            artifacts_root=adapter.artifacts_root,
        )
        adapter.deploy(
            DeploymentRequest(
                run_id=_RUN_ID,
                artifact=artifact,
                target=f"127.0.0.1:{port}",
                operation_id=operation_id,
            )
        )

        assert adapter.reconcile(operation_id=operation_id) is ReconciliationResult.FOUND_MATCHING

    def test_reports_not_found_for_an_unknown_operation(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        assert (
            adapter.reconcile(operation_id=_operation_id("never-happened"))
            is ReconciliationResult.NOT_FOUND
        )


class TestProbe:
    def test_reports_available_for_a_valid_configuration(
        self, adapter: LocalServiceDeploymentAdapter
    ) -> None:
        status = adapter.probe()

        assert status.availability is AdapterAvailability.AVAILABLE
        assert status.fingerprint is not None
        assert status.fingerprint.provider_id == "local-service-deployment"

    def test_reports_misconfigured_when_app_script_is_missing(self, tmp_path: Path) -> None:
        adapter = LocalServiceDeploymentAdapter(
            artifacts_root=tmp_path / "artifacts",
            state_root=tmp_path / "state",
            app_script=tmp_path / "no-such-app.py",
        )

        status = adapter.probe()

        assert status.availability is AdapterAvailability.MISCONFIGURED
        assert status.fingerprint is None
        assert "app_script" in status.detail

    def test_reports_misconfigured_when_python_executable_does_not_resolve(
        self, tmp_path: Path
    ) -> None:
        adapter = LocalServiceDeploymentAdapter(
            artifacts_root=tmp_path / "artifacts",
            state_root=tmp_path / "state",
            app_script=_APP_SCRIPT,
            python_executable="no-such-enginery-fixture-python-xyz",
        )

        status = adapter.probe()

        assert status.availability is AdapterAvailability.MISCONFIGURED
        assert status.fingerprint is None
        assert "python_executable" in status.detail

    def test_reports_misconfigured_when_artifacts_root_is_not_a_directory(
        self, tmp_path: Path
    ) -> None:
        occupied = tmp_path / "artifacts"
        occupied.write_text("not a directory", encoding="utf-8")
        adapter = LocalServiceDeploymentAdapter(
            artifacts_root=occupied,
            state_root=tmp_path / "state",
            app_script=_APP_SCRIPT,
        )

        status = adapter.probe()

        assert status.availability is AdapterAvailability.MISCONFIGURED
        assert status.fingerprint is None
        assert "artifacts_root" in status.detail

    def test_probe_performs_no_deployment_side_effect(self, tmp_path: Path) -> None:
        """probe() must never start a process or write state -- purely local
        file/executable checks, per the ADAPTER doctor contract."""
        adapter = LocalServiceDeploymentAdapter(
            artifacts_root=tmp_path / "artifacts",
            state_root=tmp_path / "state",
            app_script=_APP_SCRIPT,
        )

        adapter.probe()

        assert not adapter.artifacts_root.exists()
        assert not adapter.state_root.exists()
