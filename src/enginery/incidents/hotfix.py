"""Hotfix worktree creation, minimal repair application, non-vacuous
regression evidence, and emergency pull-request composition for an
incident's affected release lineage.

Uses direct, fixed subprocess ``git`` calls -- the same fixed-broker
discipline ``FixtureBuilder``/``VersionChangelogBroker`` already use for
Stage 2 -- rather than the run-scoped, coordinator-fenced
``GitWorktreeBackend`` Stage 1 uses. An emergency hotfix operates on the
controlled local-service fixture (a repository distinct from Enginery's
own), so Stage 1's multi-day, human-in-the-loop lease/fencing machinery
is disproportionate; a hotfix worktree lives only as long as one
directly-supervised emergency response.

Validation reuses the existing ``ValidationPort`` contract unchanged.
Review reuses the existing, coordinator-independent
``enginery.workflows.review`` module unchanged. Neither is redefined
here.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from enginery.application.delivery_ports import (
    ValidationPort,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
)
from enginery.application.work_ports import PullRequestRequest
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.ids import OperationId, RunId

_EMERGENCY_TITLE_PREFIX = "hotfix: "


@dataclass(frozen=True, slots=True)
class HotfixRepair:
    """A minimal, caller-authored code change applied inside a hotfix worktree."""

    file_path: str
    content: str
    commit_message: str

    def __post_init__(self) -> None:
        if not self.file_path.strip():
            raise InvalidInputError("hotfix repair file_path must be non-blank")
        if not self.commit_message.strip():
            raise InvalidInputError("hotfix repair commit_message must be non-blank")


@dataclass(frozen=True, slots=True)
class HotfixWorkspace:
    """A git worktree bound to one incident's affected release lineage."""

    root: Path
    base_revision: str
    branch: str


def create_hotfix_worktree(
    *, repository: Path, base_revision: str, branch: str, worktree_root: Path
) -> HotfixWorkspace:
    """Create a fresh worktree at ``base_revision`` on a new ``branch``.

    Fails loudly rather than silently reusing an existing worktree
    directory -- an emergency hotfix must never accidentally build on
    top of unrelated prior state.
    """
    if worktree_root.exists():
        raise ExternalConflictError(
            "hotfix worktree_root already exists", details={"path": str(worktree_root)}
        )
    _run_git(repository, "worktree", "add", "-b", branch, str(worktree_root), base_revision)
    return HotfixWorkspace(root=worktree_root, base_revision=base_revision, branch=branch)


def apply_repair(workspace: HotfixWorkspace, repair: HotfixRepair) -> str:
    """Write the repair content and commit it. Returns the new commit revision."""
    target = workspace.root / repair.file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(repair.content, encoding="utf-8")
    _run_git(workspace.root, "add", repair.file_path)
    _run_git(workspace.root, "commit", "-m", repair.commit_message)
    return _run_git(workspace.root, "rev-parse", "HEAD").strip()


def remove_hotfix_worktree(*, repository: Path, workspace: HotfixWorkspace) -> None:
    """Remove the worktree, retaining the branch and its commits for evidence."""
    _run_git(repository, "worktree", "remove", "--force", str(workspace.root))


@dataclass(frozen=True, slots=True)
class NonVacuousRegressionEvidence:
    """Proof that a regression check meaningfully distinguishes the
    unfixed and repaired revisions, not a check that trivially always
    passes or always fails."""

    unfixed_result: ValidationResult
    repaired_result: ValidationResult

    @property
    def is_non_vacuous(self) -> bool:
        return (
            self.unfixed_result.status is ValidationStatus.FAILED
            and self.repaired_result.status is ValidationStatus.PASSED
        )


def prove_non_vacuous_regression(
    validation: ValidationPort,
    *,
    run_id: RunId,
    workspace: HotfixWorkspace,
    command: tuple[str, ...],
    repaired_revision: str,
) -> NonVacuousRegressionEvidence:
    """Run the same regression command against the unfixed and repaired
    revisions inside one worktree, proving the check actually
    distinguishes them rather than passing (or failing) unconditionally.

    Leaves the worktree checked out at ``repaired_revision`` afterward.
    """
    _run_git(workspace.root, "checkout", "--detach", workspace.base_revision)
    unfixed_result = validation.validate(
        ValidationRequest(
            run_id=run_id,
            workspace_path=workspace.root,
            revision=workspace.base_revision,
            command=command,
            operation_id=_regression_operation_id(run_id, workspace.base_revision),
        )
    )
    _run_git(workspace.root, "checkout", workspace.branch)
    repaired_result = validation.validate(
        ValidationRequest(
            run_id=run_id,
            workspace_path=workspace.root,
            revision=repaired_revision,
            command=command,
            operation_id=_regression_operation_id(run_id, repaired_revision),
        )
    )
    return NonVacuousRegressionEvidence(
        unfixed_result=unfixed_result, repaired_result=repaired_result
    )


def emergency_pull_request_request(
    *, head_branch: str, base_branch: str, summary: str, operation_id: OperationId
) -> PullRequestRequest:
    """Build a conventionally-titled emergency hotfix pull-request request."""
    return PullRequestRequest(
        head_branch=head_branch,
        base_branch=base_branch,
        title=f"{_EMERGENCY_TITLE_PREFIX}{summary}",
        body=(
            f"Emergency hotfix for: {summary}\n\n"
            "Opened by the incident-to-hotfix workflow. Deployment and "
            "rollback are separately authorized and never gated on this "
            "pull request merging."
        ),
        operation_id=operation_id,
    )


def _regression_operation_id(run_id: RunId, revision: str) -> OperationId:
    payload = "\x1f".join(("hotfix-regression", str(run_id), revision))
    return OperationId(value=hashlib.sha256(payload.encode("utf-8")).hexdigest())


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExternalConflictError(
            f"git {' '.join(args)} failed", details={"stderr": result.stderr}
        )
    return result.stdout


__all__ = [
    "HotfixRepair",
    "HotfixWorkspace",
    "NonVacuousRegressionEvidence",
    "apply_repair",
    "create_hotfix_worktree",
    "emergency_pull_request_request",
    "prove_non_vacuous_regression",
    "remove_hotfix_worktree",
]
