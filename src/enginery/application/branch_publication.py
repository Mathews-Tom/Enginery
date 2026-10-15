"""Port for the host-side effect that publishes a verified branch to its remote.

A worker commits locally and never holds a remote or a credential. The branch it produced is
verified by the host, and only then does the operator side push exactly that revision, constrained
to the remote head it expects, idempotently and reconcilably like every other external effect.

A branch that changes what governs the project's own automation -- CI configuration, code
ownership, evaluator and holdout material -- is never published silently: the changed paths are
matched against a protected set first, and a match is held for an explicit operator decision.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from enginery.application.adapter_types import AdapterStatus
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_MAX_BRANCH_LENGTH = 200

DEFAULT_PROTECTED_PATH_PATTERNS: tuple[str, ...] = (
    # Hosted CI and automation: a change can run with the project's credentials on the remote.
    ".github/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    ".circleci/**",
    ".buildkite/**",
    ".azuredevops/**",
    ".azure-pipelines/**",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    "Jenkinsfile",
    ".travis.yml",
    ".appveyor.yml",
    "appveyor.yml",
    ".drone.yml",
    "bitbucket-pipelines.yml",
    "cloudbuild.yaml",
    "cloudbuild.yml",
    # Who must review what: a change can remove the required reviewer of itself.
    "CODEOWNERS",
    # Submodule definitions: a change re-points a pinned dependency to a URL that CI and every
    # fresh recursive clone then fetch.
    ".gitmodules",
    # Evaluator, holdout and factory configuration: material a candidate must not rewrite to
    # change how it is judged.
    ".enginery/**",
    "holdouts/**",
)
"""The paths a published branch may not change without an explicit operator decision.

A pattern without ``/`` matches a file name at any depth; one with ``/`` is anchored at the
repository root. ``*`` and ``?`` stay inside one path component, ``**`` crosses components, and a
trailing ``/**`` also matches the directory entry itself. Matching ignores case and Unicode
normalization form, because the checkout may live on a case- and normalization-insensitive
filesystem where a differently spelled path is the same file.
"""


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    anchored = "/" in pattern
    body = pattern
    tail = ""
    if body.endswith("/**"):
        body = body[:-3]
        tail = "(?:/.*)?"
    pieces: list[str] = []
    index = 0
    while index < len(body):
        if body.startswith("**/", index):
            pieces.append("(?:.*/)?")
            index += 3
        elif body.startswith("**", index):
            pieces.append(".*")
            index += 2
        elif body[index] == "*":
            pieces.append("[^/]*")
            index += 1
        elif body[index] == "?":
            pieces.append("[^/]")
            index += 1
        else:
            pieces.append(re.escape(body[index]))
            index += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(f"^{prefix}{''.join(pieces)}{tail}$", re.IGNORECASE | re.DOTALL)


def _normalized(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def require_protected_pattern(pattern: str) -> None:
    """Refuse a protected-path pattern that is blank, absolute, or climbs out of the repository."""
    if (
        not pattern.strip()
        or pattern != pattern.strip()
        or pattern.startswith("/")
        or "\x00" in pattern
        or "\\" in pattern
        or any(part in {"", ".", ".."} for part in pattern.removesuffix("/**").split("/"))
    ):
        raise InvalidInputError(
            "protected path pattern must be a relative path glob", details={"pattern": pattern}
        )


@dataclass(frozen=True, slots=True)
class ProtectedPathPolicy:
    """The set of path patterns a branch may not publish a change to without an operator decision.

    The defaults are always protected; ``extra`` patterns only add to them, so a request can
    widen what is held but never narrow it.
    """

    extra: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for pattern in self.extra:
            require_protected_pattern(pattern)

    @property
    def patterns(self) -> tuple[str, ...]:
        """Every protected pattern, defaults first, without duplicates."""
        return tuple(dict.fromkeys((*DEFAULT_PROTECTED_PATH_PATTERNS, *self.extra)))

    def matched(self, paths: Sequence[str]) -> tuple[str, ...]:
        """The sorted changed paths that match a protected pattern."""
        compiled = tuple(_compile_pattern(_normalized(pattern)) for pattern in self.patterns)
        return tuple(
            sorted(
                {path for path in paths if any(rule.match(_normalized(path)) for rule in compiled)}
            )
        )


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
    """Publish a verified branch revision, read what it changes, and reconcile an ambiguity."""

    def probe(self) -> AdapterStatus: ...

    def changed_paths(self, *, base_revision: str, head_revision: str) -> tuple[str, ...]:
        """Every path ``base_revision..head_revision`` adds, changes, deletes or renames.

        Read from the host-verified repository, never from a worker claim. A rename lists both
        the old and the new path.
        """
        ...

    def publish(self, request: BranchPublicationRequest) -> PublishedBranch: ...

    def reconcile(self, request: BranchPublicationRequest) -> ReconciliationResult:
        """Compare the remote with what ``request`` would have left, from the request alone."""
        ...


__all__ = [
    "DEFAULT_PROTECTED_PATH_PATTERNS",
    "BranchPublicationPort",
    "BranchPublicationRequest",
    "ProtectedPathPolicy",
    "PublishedBranch",
    "require_branch_name",
    "require_protected_pattern",
]
