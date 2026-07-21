"""A real ``DeploymentPort`` implementation for Stage 3's controlled local
HTTP service fixture.

Every deploy or rollback starts and stops a genuine subprocess bound to a
genuine TCP port, and readiness/observation poll the service's real
``/version``/``/health`` endpoints over real HTTP -- never an in-memory
simulation. ``enginery.adapters.local.LocalDeploymentFixture`` remains a
valid, separate, purely in-memory ``DeploymentPort`` double for contract
tests that need no real process; this module exists specifically because
that fixture cannot satisfy Stage 3's "actual rollback executed and
observed on the controlled target" requirement.

State for one deployment ``target`` (``host:port``) is a small JSON file
under ``state_root``, holding the currently running revision and, when a
prior deploy exists, exactly one step of history -- enough to support one
rollback per deploy without a full history stack.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
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
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

_DEFECT_MODES = frozenset({"none", "increment_off_by_one", "health_degraded"})
_READY_ENDPOINT = "/version"
_HEALTH_ENDPOINT = "/health"
_STOP_GRACE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class LocalServiceBuild:
    """One buildable revision of the local service fixture."""

    version: str
    defect_mode: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise InvalidInputError("local service build version must be non-blank")
        if self.defect_mode not in _DEFECT_MODES:
            raise InvalidInputError(
                "unknown local service defect_mode",
                details={"defect_mode": self.defect_mode, "known": sorted(_DEFECT_MODES)},
            )


def build_local_service_artifact(
    build: LocalServiceBuild, *, artifacts_root: Path
) -> ReleaseArtifact:
    """Write one immutable, content-addressed config artifact for ``build``."""
    artifacts_root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"revision": build.version, "defect_mode": build.defect_mode}, sort_keys=True
    ).encode("utf-8")
    digest = Digest.of_bytes(payload)
    (artifacts_root / f"{digest.hex_value}.json").write_bytes(payload)
    return ReleaseArtifact(
        version=build.version,
        digest=digest,
        media_type="application/vnd.enginery.local-service-config+json",
    )


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """One completed health-observation window against a deployed target."""

    target: str
    revision: str | None
    healthy: bool
    consecutive_failures: int


@dataclass(slots=True)
class LocalServiceDeploymentAdapter:
    """Deploys, observes, and rolls back the real Stage 3 local service fixture."""

    artifacts_root: Path
    state_root: Path
    app_script: Path
    python_executable: str = field(default_factory=lambda: sys.executable)
    ready_attempts: int = 30
    ready_interval_seconds: float = 0.1
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        return AdapterStatus(
            kind=ProviderKind.DEPLOYMENT,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="local-service-deployment",
                provider_version="1.0.0",
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability(name="deploy", version=1),
                    AdapterCapability(name="rollback", version=1),
                    AdapterCapability(name="observe", version=1),
                ),
            ),
            detail="local-service-deployment is available",
        )

    def deploy(self, request: DeploymentRequest) -> DeploymentReceipt:
        config = self._load_artifact_config(request.artifact)
        host, port = _parse_target(request.target)
        prior = self._read_state(request.target)
        if prior is not None:
            self._stop(prior["current"])
        config_path = str(self.artifacts_root / f"{request.artifact.digest.hex_value}.json")
        process = self._start(config_path=config_path, host=host, port=port)
        try:
            self._wait_until_ready(host=host, port=port, expected_revision=config["revision"])
        except TimeoutError as error:
            self._force_kill(process)
            raise ExternalConflictError(
                "deployment did not become healthy within the observation window",
                details={"target": request.target, "revision": config["revision"]},
            ) from error
        current = {
            "revision": config["revision"],
            "artifact_digest": str(request.artifact.digest),
            "pid": process.pid,
            "port": port,
            "host": host,
            "config_path": config_path,
        }
        self._write_state(
            request.target,
            {"current": current, "previous": prior["current"] if prior is not None else None},
        )
        self._record(request.operation_id)
        return DeploymentReceipt(
            target=request.target,
            artifact_digest=request.artifact.digest,
            deployment_id=f"deployment-{request.operation_id}",
        )

    def rollback(
        self, receipt: DeploymentReceipt, *, operation_id: OperationId
    ) -> ReconciliationResult:
        state = self._read_state(receipt.target)
        if state is None or state["current"]["artifact_digest"] != str(receipt.artifact_digest):
            return ReconciliationResult.FOUND_CONFLICTING
        previous = state["previous"]
        if previous is None:
            raise MissingPrerequisiteError(
                "no prior revision recorded for this target to roll back to",
                details={"target": receipt.target},
            )
        self._stop(state["current"])
        process = self._start(
            config_path=previous["config_path"], host=previous["host"], port=previous["port"]
        )
        try:
            self._wait_until_ready(
                host=previous["host"], port=previous["port"], expected_revision=previous["revision"]
            )
        except TimeoutError as error:
            self._force_kill(process)
            raise ExternalConflictError(
                "rollback did not become healthy within the observation window",
                details={"target": receipt.target, "revision": previous["revision"]},
            ) from error
        restored = dict(previous)
        restored["pid"] = process.pid
        self._write_state(receipt.target, {"current": restored, "previous": None})
        self._record(operation_id)
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return self._outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)

    def observe(
        self, target: str, *, attempts: int = 1, interval_seconds: float = 0.0
    ) -> HealthObservation:
        """Poll ``/health`` up to ``attempts`` times, returning the final result
        and how many consecutive failures preceded it."""
        host, port = _parse_target(target)
        consecutive_failures = 0
        revision: str | None = None
        healthy = False
        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(interval_seconds)
            try:
                revision = _get_json(host, port, _READY_ENDPOINT).get("revision")
                status = _get_json(host, port, _HEALTH_ENDPOINT).get("status")
                healthy = status == "healthy"
            except (urllib.error.URLError, OSError, ValueError):
                healthy = False
            if healthy:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
        return HealthObservation(
            target=target,
            revision=revision,
            healthy=healthy,
            consecutive_failures=consecutive_failures,
        )

    def _load_artifact_config(self, artifact: ReleaseArtifact) -> dict[str, Any]:
        path = self.artifacts_root / f"{artifact.digest.hex_value}.json"
        if not path.is_file():
            raise MissingPrerequisiteError(
                "no local service artifact registered for this digest",
                details={"digest": str(artifact.digest)},
            )
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _start(self, *, config_path: str, host: str, port: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                self.python_executable,
                str(self.app_script),
                "--config",
                config_path,
                "--host",
                host,
                "--port",
                str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _wait_until_ready(self, *, host: str, port: int, expected_revision: str) -> None:
        for attempt in range(self.ready_attempts):
            if attempt > 0:
                time.sleep(self.ready_interval_seconds)
            try:
                observed = _get_json(host, port, _READY_ENDPOINT)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if observed.get("revision") == expected_revision:
                return
        raise TimeoutError(
            f"service at {host}:{port} never reported revision {expected_revision!r}"
        )

    def _stop(self, state: dict[str, Any]) -> None:
        pid = int(state["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def _force_kill(self, process: subprocess.Popen[bytes]) -> None:
        process.terminate()
        try:
            process.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_STOP_GRACE_SECONDS)

    def _state_path(self, target: str) -> Path:
        safe = target.replace(":", "_").replace("/", "_")
        return self.state_root / f"{safe}.json"

    def _read_state(self, target: str) -> dict[str, Any] | None:
        path = self._state_path(target)
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _write_state(self, target: str, state: dict[str, Any]) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._state_path(target).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    def _record(self, operation_id: OperationId) -> None:
        self._outcomes[str(operation_id)] = ReconciliationResult.FOUND_MATCHING


def _parse_target(target: str) -> tuple[str, int]:
    if ":" not in target:
        raise InvalidInputError(
            "local service deployment target must be host:port", details={"target": target}
        )
    host, _, port_text = target.rpartition(":")
    if not host or not port_text.isdigit():
        raise InvalidInputError(
            "local service deployment target must be host:port", details={"target": target}
        )
    return host, int(port_text)


def _get_json(host: str, port: int, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=1) as response:
        data: dict[str, Any] = json.loads(response.read())
    return data


__all__ = [
    "HealthObservation",
    "LocalServiceBuild",
    "LocalServiceDeploymentAdapter",
    "build_local_service_artifact",
]
