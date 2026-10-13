"""Value validation for the branch-publication port."""

from __future__ import annotations

import pytest

from enginery.application.branch_publication import (
    DEFAULT_PROTECTED_PATH_PATTERNS,
    BranchPublicationRequest,
    ProtectedPathPolicy,
    PublishedBranch,
    require_branch_name,
    require_protected_pattern,
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


# --- protected paths -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/nested/deep/x.yml",
        ".github/CODEOWNERS",
        ".github",
        ".gitlab-ci.yml",
        "subproject/.gitlab-ci.yml",
        ".gitlab/ci/build.yml",
        ".circleci/config.yml",
        ".buildkite/pipeline.yml",
        "azure-pipelines.yml",
        "Jenkinsfile",
        "services/api/Jenkinsfile",
        "CODEOWNERS",
        "docs/CODEOWNERS",
        ".enginery/capabilities.lock.json",
        ".enginery/evaluators/x.json",
        "holdouts/case-1/expected.json",
        # Case variants: the same file on a case-insensitive filesystem.
        ".GITHUB/Workflows/CI.YML",
        ".Github/workflows/x.yml",
        "jenkinsfile",
        "codeowners",
        "HOLDOUTS/a.txt",
        ".ENGINERY/config",
        # Unicode normalization variants of a path with a non-ASCII pattern component.
        "holdouts/caf\u00e9.json",
        "holdouts/cafe\u0301.json",
    ],
)
def test_a_default_protected_path_is_matched(path: str) -> None:
    assert ProtectedPathPolicy().matched([path]) == (path,)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/app.py",
        "docs/guide.md",
        ".githubx/workflows/x.yml",
        "x.github/workflows/x.yml",
        "docs/.github/workflows/x.yml",
        "src/holdouts/readme.md",
        "src/.enginery/x",
        "CODEOWNERS.md",
        "Jenkinsfile.example",
        "my-Jenkinsfile",
        ".gitlab-ci.yml.bak",
        "github/workflows/x.yml",
    ],
)
def test_an_ordinary_path_is_not_protected(path: str) -> None:
    assert ProtectedPathPolicy().matched([path]) == ()


def test_matching_reports_the_sorted_protected_subset_of_a_mixed_change() -> None:
    paths = [
        "src/a.py",
        ".github/workflows/b.yml",
        "README.md",
        "CODEOWNERS",
        ".github/workflows/b.yml",
    ]

    assert ProtectedPathPolicy().matched(paths) == (".github/workflows/b.yml", "CODEOWNERS")
    assert ProtectedPathPolicy().matched([]) == ()


def test_extra_patterns_widen_the_set_and_never_narrow_the_defaults() -> None:
    policy = ProtectedPathPolicy(("deploy/**", "*.tf", "infra/*/prod.yml"))

    assert (
        policy.patterns[: len(DEFAULT_PROTECTED_PATH_PATTERNS)] == DEFAULT_PROTECTED_PATH_PATTERNS
    )
    assert policy.matched(
        ["deploy/a/b.sh", "main.tf", "modules/net/main.tf", "infra/eu/prod.yml", ".github/x.yml"]
    ) == (
        ".github/x.yml",
        "deploy/a/b.sh",
        "infra/eu/prod.yml",
        "main.tf",
        "modules/net/main.tf",
    )
    assert policy.matched(["infra/eu/west/prod.yml", "terraform/x.txt"]) == ()
    assert ProtectedPathPolicy(("holdouts/**",)).patterns == DEFAULT_PROTECTED_PATH_PATTERNS


def test_a_glob_star_stays_in_one_component_and_a_double_star_crosses_them() -> None:
    policy = ProtectedPathPolicy(("a/*/c.txt", "d/**/e.txt", "f?.txt"))

    assert policy.matched(["a/b/c.txt"]) == ("a/b/c.txt",)
    assert policy.matched(["a/b/x/c.txt", "a/c.txt"]) == ()
    assert policy.matched(["d/e.txt", "d/x/y/e.txt"]) == ("d/e.txt", "d/x/y/e.txt")
    assert policy.matched(["f1.txt", "f12.txt", "f/.txt"]) == ("f1.txt",)


def test_paths_with_regex_metacharacters_and_newlines_cannot_escape_or_break_matching() -> None:
    policy = ProtectedPathPolicy(("weird(name)+[x].txt",))

    assert policy.matched(["weird(name)+[x].txt"]) == ("weird(name)+[x].txt",)
    assert policy.matched(["weirdname+x.txt", "weird(name)+x.txt"]) == ()
    assert policy.matched(["src/line\nbreak.py", ".github/new\nline.yml"]) == (
        ".github/new\nline.yml",
    )


@pytest.mark.parametrize(
    "pattern", ["", " ", "/abs/**", "../up/**", "a/../b", "a//b", "tail ", "back\\slash", "nul\x00"]
)
def test_a_protected_pattern_must_be_a_relative_path_glob(pattern: str) -> None:
    with pytest.raises(InvalidInputError, match="relative path glob"):
        require_protected_pattern(pattern)
    with pytest.raises(InvalidInputError):
        ProtectedPathPolicy((pattern,))
