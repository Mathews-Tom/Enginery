"""Provider-neutral ports for work intake, workers, workspaces, and source control."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from enginery.application.adapter_types import AdapterStatus, NormalizedAdapterEvent
from enginery.domain.digests import Digest
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.work_item import WorkItem


@dataclass(frozen=True, slots=True)
class WorkLedgerSnapshot:
    """A provider-neutral source snapshot already normalized into a work item."""

    work_item: WorkItem
    source_revision: str

    def __post_init__(self) -> None:
        if not self.source_revision.strip():
            raise ValueError("work ledger source_revision must be non-blank")


@dataclass(frozen=True, slots=True)
class LifecycleProjection:
    """A concise lifecycle update for one normalized external work reference."""

    run_id: RunId
    external_reference: str
    state: str
    evidence_digest: Digest | None

    def __post_init__(self) -> None:
        if not self.external_reference.strip():
            raise ValueError("lifecycle projection external_reference must be non-blank")
        if not self.state.strip():
            raise ValueError("lifecycle projection state must be non-blank")


class WorkLedgerPort(Protocol):
    """Ingest and publish normalized work state without provider types."""

    def probe(self) -> AdapterStatus: ...

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot: ...

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class HarnessTask:
    """The bounded task envelope passed to an agent-harness adapter."""

    run_id: RunId
    node_id: NodeId
    attempt_id: NodeAttemptId
    operation_id: OperationId
    workspace_path: Path
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    permitted_capabilities: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    time_budget_seconds: int
    cost_budget: Decimal | None

    def __post_init__(self) -> None:
        if not self.workspace_path.is_absolute():
            raise ValueError("harness task workspace_path must be absolute")
        if not self.objective.strip():
            raise ValueError("harness task objective must be non-blank")
        if self.time_budget_seconds < 1:
            raise ValueError("harness task time budget must be positive")
        if self.cost_budget is not None and self.cost_budget < 0:
            raise ValueError("harness task cost budget cannot be negative")
        for values, field_name in (
            (self.acceptance_criteria, "acceptance_criteria"),
            (self.constraints, "constraints"),
            (self.permitted_capabilities, "permitted_capabilities"),
            (self.evidence_requirements, "evidence_requirements"),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"harness task {field_name} must contain non-blank values")


@dataclass(frozen=True, slots=True)
class HarnessSession:
    """A provider-neutral running worker identity."""

    session_id: str
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("harness session_id must be non-blank")


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """A terminal harness outcome with artifact-backed evidence."""

    session_id: str
    terminal_status: str
    output_digests: tuple[Digest, ...]

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.terminal_status.strip():
            raise ValueError("harness result identity and terminal_status must be non-blank")


class HarnessPort(Protocol):
    """Run, observe, and interrupt a normalized harness task."""

    def probe(self) -> AdapterStatus: ...

    def start(self, task: HarnessTask) -> HarnessSession: ...

    def events(self, session: HarnessSession) -> Iterator[NormalizedAdapterEvent]: ...

    def result(self, session: HarnessSession) -> HarnessResult: ...

    def cancel(
        self, session: HarnessSession, *, operation_id: OperationId
    ) -> ReconciliationResult: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class WorkspaceRequest:
    """An exclusive workspace materialization request at one source revision."""

    run_id: RunId
    repository_id: str
    repository_path: Path
    base_revision: str
    operation_id: OperationId
    permitted_environment_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.repository_id.strip() or not self.base_revision.strip():
            raise ValueError("workspace repository identity and base revision must be non-blank")
        if not self.repository_path.is_absolute():
            raise ValueError("workspace repository_path must be absolute")
        if any(not key.strip() for key in self.permitted_environment_keys):
            raise ValueError("workspace environment keys must be non-blank")


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    """An exclusive materialized workspace."""

    reservation_id: str
    repository_id: str
    path: Path
    base_revision: str

    def __post_init__(self) -> None:
        if not self.reservation_id.strip() or not self.repository_id.strip():
            raise ValueError("workspace reservation and repository identifiers must be non-blank")
        if not self.path.is_absolute() or not self.base_revision.strip():
            raise ValueError("workspace path must be absolute and base revision non-blank")


class WorkspacePort(Protocol):
    """Reserve, inspect, and release isolated workspaces."""

    def probe(self) -> AdapterStatus: ...

    def create(self, request: WorkspaceRequest) -> WorkspaceHandle: ...

    def retain(
        self, workspace: WorkspaceHandle, *, operation_id: OperationId
    ) -> ReconciliationResult: ...

    def cleanup(
        self, workspace: WorkspaceHandle, *, operation_id: OperationId
    ) -> ReconciliationResult: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """A resolved immutable revision and its tree digest."""

    revision: str
    tree_digest: Digest

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValueError("source revision must be non-blank")


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """A digest of changed paths at one revision."""

    revision: str
    changed_paths: tuple[str, ...]
    diff_digest: Digest

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValueError("change set revision must be non-blank")
        if any(not path.strip() for path in self.changed_paths):
            raise ValueError("change set paths must be non-blank")


@dataclass(frozen=True, slots=True)
class SourceBranch:
    """A policy-approved branch identity created through source control."""

    name: str
    head: SourceRevision

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("source branch name must be non-blank")


@dataclass(frozen=True, slots=True)
class PullRequestRequest:
    """A policy-approved idempotent request to create or update one pull request."""

    head_branch: str
    base_branch: str
    title: str
    body: str
    operation_id: OperationId

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.head_branch, self.base_branch, self.title, self.body)
        ):
            raise ValueError("pull request request fields must be non-blank")
        if self.head_branch == self.base_branch:
            raise ValueError("pull request head and base branches must differ")


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    """Normalized source-host pull-request metadata bound to exact revisions."""

    number: int
    url: str
    state: str
    head_branch: str
    head_revision: str
    base_branch: str
    base_revision: str

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("pull request number must be positive")
        if any(
            not value.strip()
            for value in (
                self.url,
                self.state,
                self.head_branch,
                self.head_revision,
                self.base_branch,
                self.base_revision,
            )
        ):
            raise ValueError("pull request snapshot fields must be non-blank")


class PullRequestPort(Protocol):
    """Create, inspect, and reconcile normalized pull requests."""

    def probe(self) -> AdapterStatus: ...

    def create_or_update(self, request: PullRequestRequest) -> PullRequestSnapshot: ...

    def get(self, number: int) -> PullRequestSnapshot: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


class SourceControlPort(Protocol):
    """Resolve source state and perform policy-approved Git mutations."""

    def probe(self) -> AdapterStatus: ...

    def resolve_revision(self, revision: str) -> SourceRevision: ...

    def changed_paths(self, base: SourceRevision, head: SourceRevision) -> ChangeSet: ...

    def create_branch(
        self, name: str, *, base: SourceRevision, operation_id: OperationId
    ) -> SourceBranch: ...

    def commit(
        self,
        branch: SourceBranch,
        message: str,
        *,
        operation_id: OperationId,
    ) -> SourceRevision: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


__all__ = [
    "ChangeSet",
    "HarnessPort",
    "HarnessResult",
    "HarnessSession",
    "HarnessTask",
    "LifecycleProjection",
    "PullRequestPort",
    "PullRequestRequest",
    "PullRequestSnapshot",
    "SourceBranch",
    "SourceControlPort",
    "SourceRevision",
    "WorkLedgerPort",
    "WorkLedgerSnapshot",
    "WorkspaceHandle",
    "WorkspacePort",
    "WorkspaceRequest",
]
