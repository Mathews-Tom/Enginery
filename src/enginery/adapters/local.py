"""Deterministic local implementations of the application adapter ports."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    NormalizedAdapterEvent,
    ProviderKind,
)
from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    DeploymentReceipt,
    DeploymentRequest,
    PublicationReceipt,
    PublicationRequest,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
)
from enginery.application.work_ports import (
    ChangeSet,
    HarnessResult,
    HarnessSession,
    HarnessTask,
    LifecycleProjection,
    SourceBranch,
    SourceRevision,
    WorkLedgerSnapshot,
    WorkspaceHandle,
    WorkspaceRequest,
)
from enginery.domain.digests import Digest
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.redaction import redact_credential_shaped_text


def _status(kind: ProviderKind, provider_id: str, capabilities: tuple[str, ...]) -> AdapterStatus:
    return AdapterStatus(
        kind=kind,
        availability=AdapterAvailability.AVAILABLE,
        fingerprint=AdapterFingerprint(
            provider_id=provider_id,
            provider_version="1.0.0",
            api_version=ADAPTER_API_VERSION,
            capabilities=tuple(AdapterCapability(name=name, version=1) for name in capabilities),
        ),
        detail=f"{provider_id} is available",
    )


def _record(outcomes: dict[str, ReconciliationResult], operation_id: OperationId) -> None:
    outcomes[str(operation_id)] = ReconciliationResult.FOUND_MATCHING


def _reconcile(
    outcomes: Mapping[str, ReconciliationResult], operation_id: OperationId
) -> ReconciliationResult:
    return outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)


@dataclass(slots=True)
class LocalWorkLedger:
    """An in-memory deterministic work-ledger fixture."""

    snapshots: Mapping[str, WorkLedgerSnapshot]
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    projections: list[LifecycleProjection] = field(default_factory=list, init=False)

    def probe(self) -> AdapterStatus:
        return _status(ProviderKind.WORK_LEDGER, "local-work-ledger", ("fetch", "publish"))

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        return self.snapshots[external_reference]

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        _record(self._outcomes, operation_id)
        self.projections.append(projection)
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)


@dataclass(slots=True)
class ScriptedHarness:
    """A deterministic harness whose emitted events and terminal result are scripted."""

    emitted_events: tuple[NormalizedAdapterEvent, ...]
    terminal_status: str = "succeeded"
    output_digests: tuple[Digest, ...] = ()
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    _sessions: dict[str, HarnessSession] = field(default_factory=dict, init=False)
    _results: dict[str, HarnessResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        return _status(ProviderKind.HARNESS, "scripted-harness", ("events", "cancel"))

    def start(self, task: HarnessTask) -> HarnessSession:
        session = HarnessSession(
            session_id=f"session-{task.operation_id}", operation_id=task.operation_id
        )
        self._sessions[session.session_id] = session
        self._results[session.session_id] = HarnessResult(
            session_id=session.session_id,
            terminal_status=self.terminal_status,
            output_digests=self.output_digests,
        )
        _record(self._outcomes, task.operation_id)
        return session

    def events(self, session: HarnessSession) -> Iterator[NormalizedAdapterEvent]:
        self._require_session(session)
        yield from self.emitted_events

    def result(self, session: HarnessSession) -> HarnessResult:
        self._require_session(session)
        return self._results[session.session_id]

    def cancel(self, session: HarnessSession, *, operation_id: OperationId) -> ReconciliationResult:
        self._require_session(session)
        _record(self._outcomes, operation_id)
        prior = self._results[session.session_id]
        self._results[session.session_id] = HarnessResult(
            session_id=session.session_id,
            terminal_status="cancelled",
            output_digests=prior.output_digests,
        )
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)

    def _require_session(self, session: HarnessSession) -> None:
        if self._sessions.get(session.session_id) != session:
            raise KeyError(f"unknown scripted harness session {session.session_id!r}")


@dataclass(slots=True)
class LocalWorkspace:
    """A Git-worktree workspace implementation for local repositories."""

    root: Path
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    _workspaces: dict[str, WorkspaceHandle] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        return _status(
            ProviderKind.WORKSPACE, "local-git-worktree", ("create", "cleanup", "retain")
        )

    def create(self, request: WorkspaceRequest) -> WorkspaceHandle:
        reservation_id = f"workspace-{request.operation_id}"
        path = self.root / reservation_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            request.repository_path, "worktree", "add", "--detach", str(path), request.base_revision
        )
        workspace = WorkspaceHandle(
            reservation_id=reservation_id,
            repository_id=request.repository_id,
            path=path,
            base_revision=request.base_revision,
        )
        self._workspaces[reservation_id] = workspace
        _record(self._outcomes, request.operation_id)
        return workspace

    def retain(
        self, workspace: WorkspaceHandle, *, operation_id: OperationId
    ) -> ReconciliationResult:
        self._require_workspace(workspace)
        _record(self._outcomes, operation_id)
        return ReconciliationResult.FOUND_MATCHING

    def cleanup(
        self, workspace: WorkspaceHandle, *, operation_id: OperationId
    ) -> ReconciliationResult:
        self._require_workspace(workspace)
        self._git(workspace.path, "worktree", "remove", "--force", str(workspace.path))
        self._workspaces.pop(workspace.reservation_id)
        _record(self._outcomes, operation_id)
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)

    def _require_workspace(self, workspace: WorkspaceHandle) -> None:
        if self._workspaces.get(workspace.reservation_id) != workspace:
            raise KeyError(f"unknown workspace {workspace.reservation_id!r}")

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)
        return result.stdout.strip()


@dataclass(slots=True)
class LocalGit:
    """A local Git source-control adapter with deterministic reconciliation records."""

    repository: Path
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        try:
            self._git("rev-parse", "--git-dir")
        except subprocess.CalledProcessError:
            return AdapterStatus(
                kind=ProviderKind.SOURCE_CONTROL,
                availability=AdapterAvailability.MISCONFIGURED,
                fingerprint=None,
                detail="local Git repository is unavailable",
            )
        return _status(ProviderKind.SOURCE_CONTROL, "local-git", ("branches", "commits", "diff"))

    def resolve_revision(self, revision: str) -> SourceRevision:
        resolved = self._git("rev-parse", revision)
        tree = self._git("rev-parse", f"{resolved}^{{tree}}")
        return SourceRevision(revision=resolved, tree_digest=Digest.of_bytes(tree.encode()))

    def changed_paths(self, base: SourceRevision, head: SourceRevision) -> ChangeSet:
        paths = tuple(
            filter(
                None, self._git("diff", "--name-only", base.revision, head.revision).splitlines()
            )
        )
        diff = self._git("diff", "--binary", base.revision, head.revision)
        return ChangeSet(
            revision=head.revision, changed_paths=paths, diff_digest=Digest.of_bytes(diff.encode())
        )

    def create_branch(
        self, name: str, *, base: SourceRevision, operation_id: OperationId
    ) -> SourceBranch:
        self._git("branch", name, base.revision)
        _record(self._outcomes, operation_id)
        return SourceBranch(name=name, head=base)

    def commit(
        self, branch: SourceBranch, message: str, *, operation_id: OperationId
    ) -> SourceRevision:
        if not message.strip():
            raise ValueError("commit message must be non-blank")
        self._git("switch", branch.name)
        self._git("add", "--all")
        self._git("commit", "-m", message)
        _record(self._outcomes, operation_id)
        return self.resolve_revision("HEAD")

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ("git", *args), cwd=self.repository, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()


@dataclass(slots=True)
class LocalValidation:
    """A local argument-vector validator that digests redacted process output."""

    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        return _status(ProviderKind.VALIDATION, "local-validation", ("commands",))

    def validate(self, request: ValidationRequest) -> ValidationResult:
        try:
            result = subprocess.run(
                request.command,
                cwd=request.workspace_path,
                check=False,
                capture_output=True,
                text=True,
            )
            output = result.stdout + result.stderr
            status = ValidationStatus.PASSED if result.returncode == 0 else ValidationStatus.FAILED
            exit_code = result.returncode
        except OSError as error:
            output = str(error)
            status = ValidationStatus.ERRORED
            exit_code = 127
        _record(self._outcomes, request.operation_id)
        return ValidationResult(
            revision=request.revision,
            status=status,
            exit_code=exit_code,
            output_digest=Digest.of_bytes(redact_credential_shaped_text(output).encode()),
        )

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)


@dataclass(slots=True)
class LocalPublication:
    """A filesystem-backed fixture publication target with destination verification."""

    root: Path
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        return _status(ProviderKind.RELEASE, "local-publication", ("publish", "verify"))

    def publish(self, request: PublicationRequest) -> PublicationReceipt:
        destination = self.root / request.destination / request.artifact.version
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"digest": str(request.artifact.digest)}, sort_keys=True).encode()
        destination.write_bytes(payload)
        _record(self._outcomes, request.operation_id)
        return PublicationReceipt(
            destination=request.destination,
            version=request.artifact.version,
            artifact_digest=request.artifact.digest,
        )

    def verify(self, receipt: PublicationReceipt) -> PublicationReceipt:
        payload = json.loads((self.root / receipt.destination / receipt.version).read_text())
        if payload != {"digest": str(receipt.artifact_digest)}:
            raise ValueError("published artifact digest does not match destination evidence")
        return receipt

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)


@dataclass(slots=True)
class LocalDeploymentFixture:
    """A controlled deployment fixture; it does not contact external services."""

    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    _deployments: dict[str, DeploymentReceipt] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        return _status(ProviderKind.DEPLOYMENT, "local-deployment-fixture", ("deploy", "rollback"))

    def deploy(self, request: DeploymentRequest) -> DeploymentReceipt:
        receipt = DeploymentReceipt(
            target=request.target,
            artifact_digest=request.artifact.digest,
            deployment_id=f"deployment-{request.operation_id}",
        )
        self._deployments[receipt.deployment_id] = receipt
        _record(self._outcomes, request.operation_id)
        return receipt

    def rollback(
        self, receipt: DeploymentReceipt, *, operation_id: OperationId
    ) -> ReconciliationResult:
        if self._deployments.get(receipt.deployment_id) != receipt:
            return ReconciliationResult.FOUND_CONFLICTING
        self._deployments.pop(receipt.deployment_id)
        _record(self._outcomes, operation_id)
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return _reconcile(self._outcomes, operation_id)


@dataclass(frozen=True, slots=True)
class LocalCapabilitySource:
    """An explicit repository-local capability inventory; no runtime discovery mechanism."""

    capabilities: tuple[CapabilityDescriptor, ...]

    def probe(self) -> AdapterStatus:
        return _status(
            ProviderKind.CAPABILITY_SOURCE, "local-capability-source", ("discover", "resolve")
        )

    def discover(self) -> tuple[CapabilityDescriptor, ...]:
        return self.capabilities

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        return next(
            (item for item in self.capabilities if item.name == name and item.version == version),
            None,
        )


__all__ = [
    "LocalCapabilitySource",
    "LocalDeploymentFixture",
    "LocalGit",
    "LocalPublication",
    "LocalValidation",
    "LocalWorkLedger",
    "LocalWorkspace",
    "ScriptedHarness",
]
