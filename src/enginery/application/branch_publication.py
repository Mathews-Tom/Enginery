"""Port for the host-side effect that publishes a verified branch to its remote.

A worker commits locally and never holds a remote or a credential. The branch it produced is
verified by the host, and only then does the operator side push exactly that revision, constrained
to the remote head it expects, idempotently and reconcilably like every other external effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from enginery.application.adapter_types import AdapterStatus
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_MAX_BRANCH_LENGTH = 200


def require_branch_name(branch: str) -> None:
    """Refuse a name that is not a plain, safe ``refs/heads`` branch name."""
    if (
        not branch
        or len(branch) > _MAX_BRANCH_LENGTH
        or branch.startswith(("-", "/", "refs/"))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch == "@"
        or _BRANCH_FORBIDDEN.search(branch) is not None
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise InvalidInputError("branch name is not a safe branch name", details={"branch": branch})


@dataclass(frozen=True, slots=True)
class BranchPublicationRequest:
    """One expected-head-constrained request to publish a verified branch revision.

    ``expected_remote_revision`` is the revision the remote branch must currently be at, or
    ``None`` when it must not exist yet. A remote branch at any other revision is never
    overwritten.
    """

    repository_id: str
    branch: str
    head_revision: str
    expected_remote_revision: str | None
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not self.repository_id.strip():
            raise InvalidInputError("branch publication repository identity must be non-blank")
        require_branch_name(self.branch)
        if _OBJECT_ID.fullmatch(self.head_revision) is None:
            raise InvalidInputError(
                "branch publication head revision must be a full object id",
                details={"head_revision": self.head_revision},
            )
        if (
            self.expected_remote_revision is not None
            and _OBJECT_ID.fullmatch(self.expected_remote_revision) is None
        ):
            raise InvalidInputError(
                "branch publication expected remote revision must be a full object id or absent",
                details={"expected_remote_revision": self.expected_remote_revision},
            )


@dataclass(frozen=True, slots=True)
class PublishedBranch:
    """The remote branch and the exact revision it was observed at after publication."""

    branch: str
    revision: str

    def __post_init__(self) -> None:
        require_branch_name(self.branch)
        if _OBJECT_ID.fullmatch(self.revision) is None:
            raise InvalidInputError(
                "published branch revision must be a full object id",
                details={"revision": self.revision},
            )


class BranchPublicationPort(Protocol):
    """Publish a verified branch revision and reconcile an ambiguous publication."""

    def probe(self) -> AdapterStatus: ...

    def publish(self, request: BranchPublicationRequest) -> PublishedBranch: ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult: ...


__all__ = [
    "BranchPublicationPort",
    "BranchPublicationRequest",
    "PublishedBranch",
    "require_branch_name",
]
