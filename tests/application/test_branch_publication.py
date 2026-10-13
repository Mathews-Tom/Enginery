"""Value validation for the branch-publication port."""

from __future__ import annotations

import pytest

from enginery.application.branch_publication import (
    BranchPublicationRequest,
    PublishedBranch,
    require_branch_name,
)
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import OperationId

HEAD = "a" * 40
OTHER = "b" * 64


def _request(**overrides: object) -> BranchPublicationRequest:
    fields: dict[str, object] = {
        "repository_id": "org/repo",
        "branch": "enginery/run-1",
        "head_revision": HEAD,
        "expected_remote_revision": None,
        "operation_id": OperationId("publish:run-1"),
    }
    fields.update(overrides)
    return BranchPublicationRequest(**fields)  # type: ignore[arg-type]


def test_a_request_binds_one_branch_one_revision_and_its_expectation() -> None:
    first = _request()
    repair = _request(head_revision=OTHER, expected_remote_revision=HEAD)

    assert first.expected_remote_revision is None
    assert repair.expected_remote_revision == HEAD
    assert repair.head_revision == OTHER


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-delete",
        "/leading",
        "trailing/",
        "refs/heads/x",
        "a..b",
        "a//b",
        "a b",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a@{b",
        "@",
        "ends.lock",
        "dir/.hidden",
        "dir/part.lock/x",
        "name.",
        "tab\tname",
        "x" * 201,
    ],
)
def test_an_unsafe_branch_name_is_refused(branch: str) -> None:
    with pytest.raises(InvalidInputError, match="safe branch name"):
        require_branch_name(branch)
    with pytest.raises(InvalidInputError):
        _request(branch=branch)
    with pytest.raises(InvalidInputError):
        PublishedBranch(branch=branch, revision=HEAD)


@pytest.mark.parametrize("revision", ["", "abc123", "A" * 40, "g" * 40, HEAD + "0", "HEAD", "main"])
def test_only_a_full_lowercase_object_id_is_a_revision(revision: str) -> None:
    with pytest.raises(InvalidInputError, match="full object id"):
        _request(head_revision=revision)
    with pytest.raises(InvalidInputError, match="full object id"):
        _request(expected_remote_revision=revision)
    with pytest.raises(InvalidInputError, match="full object id"):
        PublishedBranch(branch="enginery/run-1", revision=revision)


def test_a_blank_repository_identity_is_refused() -> None:
    with pytest.raises(InvalidInputError, match="repository identity"):
        _request(repository_id="  ")
