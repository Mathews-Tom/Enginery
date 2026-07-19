from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from enginery.application.work_ports import (
    ChangeSet,
    HarnessResult,
    HarnessSession,
    HarnessTask,
    LifecycleProjection,
    PullRequestRequest,
    PullRequestSnapshot,
    SourceBranch,
    SourceRevision,
    WorkLedgerSnapshot,
    WorkspaceHandle,
    WorkspaceRequest,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId, WorkItemId
from enginery.domain.work_item import WorkItem, WorkItemState


def _work_item() -> WorkItem:
    return WorkItem(
        id=WorkItemId("wi-1"),
        work_kind=WorkKind.ISSUE,
        source_provider="local-ledger",
        external_reference="work-1",
        source_snapshot_reference="snapshot-1",
        title="Implement adapter ports",
        objective="Create typed provider-neutral contracts",
        acceptance_criteria=("ports are typed",),
        constraints=("no provider SDK",),
        risk_class=RiskClass.LOW,
        repository_targets=("org/repo",),
        dependencies=(),
        state=WorkItemState.READY,
        aggregate_version=0,
    )


def _task(**overrides: object) -> HarnessTask:
    defaults: dict[str, object] = {
        "run_id": RunId("run-1"),
        "node_id": NodeId("implement"),
        "attempt_id": NodeAttemptId("attempt-1"),
        "operation_id": OperationId("op-start-1"),
        "workspace_path": Path("/tmp/workspace"),
        "objective": "Implement the bounded task",
        "acceptance_criteria": ("tests pass",),
        "constraints": ("no network",),
        "permitted_capabilities": ("repository-read",),
        "evidence_requirements": ("test report",),
        "time_budget_seconds": 300,
        "cost_budget": Decimal("1.50"),
    }
    defaults.update(overrides)
    return HarnessTask(**defaults)  # type: ignore[arg-type]


def test_harness_task_rejects_relative_workspace() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _task(workspace_path=Path("workspace"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("objective", " "),
        ("acceptance_criteria", (" ",)),
        ("constraints", (" ",)),
        ("permitted_capabilities", (" ",)),
        ("evidence_requirements", (" ",)),
    ],
)
def test_harness_task_rejects_blank_contract_text(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        _task(**{field: value})


@pytest.mark.parametrize("time_budget_seconds", [0, -1])
def test_harness_task_rejects_non_positive_time_budget(time_budget_seconds: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        _task(time_budget_seconds=time_budget_seconds)


def test_harness_task_rejects_negative_cost_budget() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _task(cost_budget=Decimal("-0.01"))


def test_workspace_request_binds_exact_repository_revision_and_operation() -> None:
    request = WorkspaceRequest(
        run_id=RunId("run-1"),
        repository_id="org/repo",
        repository_path=Path("/tmp/repository"),
        base_revision="deadbeef",
        operation_id=OperationId("op-workspace-1"),
        permitted_environment_keys=("PATH",),
    )

    assert request.base_revision == "deadbeef"
    assert request.operation_id == OperationId("op-workspace-1")


def test_workspace_handle_rejects_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        WorkspaceHandle(
            reservation_id="reservation-1",
            repository_id="org/repo",
            path=Path("workspace"),
            base_revision="deadbeef",
        )


def test_source_values_retain_revision_and_tree_identity() -> None:
    revision = SourceRevision("deadbeef", Digest.of_bytes(b"tree"))
    branch = SourceBranch("enginery/run-1", revision)

    assert branch.head.tree_digest == Digest.of_bytes(b"tree")


def test_lifecycle_projection_rejects_blank_identity() -> None:
    with pytest.raises(ValueError, match="external_reference"):
        LifecycleProjection(
            run_id=RunId("run-1"),
            external_reference=" ",
            state="active",
            evidence_digest=None,
        )
    with pytest.raises(ValueError, match="state"):
        LifecycleProjection(
            run_id=RunId("run-1"),
            external_reference="org/repository#1",
            state=" ",
            evidence_digest=None,
        )


@pytest.mark.parametrize(
    ("repository_id", "repository_path", "base_revision", "environment_keys"),
    [
        (" ", Path("/tmp/repository"), "deadbeef", ("PATH",)),
        ("org/repo", Path("repository"), "deadbeef", ("PATH",)),
        ("org/repo", Path("/tmp/repository"), " ", ("PATH",)),
        ("org/repo", Path("/tmp/repository"), "deadbeef", (" ",)),
    ],
)
def test_workspace_request_rejects_invalid_identity(
    repository_id: str,
    repository_path: Path,
    base_revision: str,
    environment_keys: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        WorkspaceRequest(
            run_id=RunId("run-1"),
            repository_id=repository_id,
            repository_path=repository_path,
            base_revision=base_revision,
            operation_id=OperationId("op-workspace-1"),
            permitted_environment_keys=environment_keys,
        )


@pytest.mark.parametrize(
    ("reservation_id", "repository_id", "base_revision"),
    [
        (" ", "org/repo", "deadbeef"),
        ("reservation-1", " ", "deadbeef"),
        ("reservation-1", "org/repo", " "),
    ],
)
def test_workspace_handle_rejects_invalid_identity(
    reservation_id: str, repository_id: str, base_revision: str
) -> None:
    with pytest.raises(ValueError):
        WorkspaceHandle(
            reservation_id=reservation_id,
            repository_id=repository_id,
            path=Path("/tmp/workspace"),
            base_revision=base_revision,
        )


def test_work_ledger_snapshot_rejects_blank_source_revision() -> None:
    with pytest.raises(ValueError, match="source_revision"):
        WorkLedgerSnapshot(work_item=_work_item(), source_revision=" ")


@pytest.mark.parametrize(
    ("session_id", "terminal_status"),
    [
        (" ", "succeeded"),
        ("session-1", " "),
    ],
)
def test_harness_result_rejects_blank_terminal_identity(
    session_id: str, terminal_status: str
) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        HarnessResult(session_id=session_id, terminal_status=terminal_status, outputs=())


def test_harness_session_rejects_blank_identity() -> None:
    with pytest.raises(ValueError, match="session_id"):
        HarnessSession(session_id=" ", operation_id=OperationId("op-start-1"))


@pytest.mark.parametrize(
    ("revision", "changed_paths"),
    [
        (" ", ("src/enginery/application/work_ports.py",)),
        ("deadbeef", (" ",)),
    ],
)
def test_change_set_rejects_invalid_revision_or_paths(
    revision: str, changed_paths: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        ChangeSet(
            revision=revision,
            changed_paths=changed_paths,
            diff_digest=Digest.of_bytes(b"diff"),
        )


def test_source_revision_rejects_blank_revision() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        SourceRevision(" ", Digest.of_bytes(b"tree"))


def test_source_branch_rejects_blank_name() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        SourceBranch(" ", SourceRevision("deadbeef", Digest.of_bytes(b"tree")))


def test_pull_request_request_rejects_same_head_and_base() -> None:
    with pytest.raises(ValueError, match="differ"):
        PullRequestRequest(
            head_branch="main",
            base_branch="main",
            title="Update provider",
            body="Make a safe update.",
            operation_id=OperationId("pr-create-1"),
        )


def test_pull_request_snapshot_rejects_blank_revisions() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        PullRequestSnapshot(
            number=1,
            url="https://github.com/example/repository/pull/1",
            state="open",
            head_branch="feature",
            head_revision=" ",
            base_branch="main",
            base_revision="b" * 40,
        )
