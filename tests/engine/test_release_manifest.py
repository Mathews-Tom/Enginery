"""Tests for enginery.engine.release_manifest."""

from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, InvalidInputError
from enginery.domain.ids import PlanId, PlanMilestoneId, StackId
from enginery.domain.stack import Stack, StackSlice, StackSliceState
from enginery.engine.release_manifest import (
    ReleaseManifest,
    ReleaseTarget,
    VersionChangelogBroker,
    constituent_work_merged,
    known_versions_from_changelog,
    validate_release_target,
)


def _stack(*, all_merged: bool) -> Stack:
    state = StackSliceState.MERGED if all_merged else StackSliceState.MERGE_READY

    slice_one = StackSlice(
        milestone_id=PlanMilestoneId("m1"),
        position=1,
        base_ref="main",
        branch_ref="fixture/m1",
        state=StackSliceState.MERGED,
        head_revision="m1-rev1",
    )
    slice_two = StackSlice(
        milestone_id=PlanMilestoneId("m2"),
        position=2,
        base_ref="fixture/m1",
        branch_ref="fixture/m2",
        state=state,
        head_revision="m2-rev1",
        ci_evidence_digest=Digest.of_bytes(b"ci-passed") if not all_merged else None,
    )
    return Stack(
        id=StackId("stack-1"),
        plan_id=PlanId("plan-1"),
        base_ref="main",
        slices={slice_one.milestone_id: slice_one, slice_two.milestone_id: slice_two},
    )


def _fixture_root(tmp_path: Path, *, name: str = "enginery-stage2-fixture") -> Path:
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\ndescription = "test"\n',
        encoding="utf-8",
    )
    return root


def test_release_target_rejects_invalid_semver() -> None:
    with pytest.raises(InvalidInputError):
        ReleaseTarget(distribution_name="enginery-stage2-fixture", version="not-a-version")


def test_validate_release_target_rejects_product_name() -> None:
    target = ReleaseTarget(distribution_name="enginery", version="0.1.0")

    with pytest.raises(ExternalConflictError, match="product's own"):
        validate_release_target(target, known_versions=frozenset())


def test_validate_release_target_rejects_known_version() -> None:
    target = ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0")

    with pytest.raises(ExternalConflictError, match="already recorded"):
        validate_release_target(target, known_versions=frozenset({"0.1.0"}))


def test_validate_release_target_allows_new_name_and_version() -> None:
    target = ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0")

    validate_release_target(target, known_versions=frozenset({"0.0.9"}))  # does not raise


def test_constituent_work_merged() -> None:
    assert constituent_work_merged(_stack(all_merged=True)) is True
    assert constituent_work_merged(_stack(all_merged=False)) is False


def test_known_versions_from_changelog_parses_headers(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## 0.2.0\n\nSecond.\n\n## 0.1.0\n\nFirst.\n", encoding="utf-8")

    assert known_versions_from_changelog(path) == frozenset({"0.2.0", "0.1.0"})


def test_known_versions_from_changelog_missing_file_returns_empty(tmp_path: Path) -> None:
    assert known_versions_from_changelog(tmp_path / "CHANGELOG.md") == frozenset()


def test_broker_refuses_when_stack_not_fully_merged(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    broker = VersionChangelogBroker(fixture_root=root)
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Initial Stage 2 fixture release.",
    )

    with pytest.raises(ExternalConflictError, match="merged"):
        broker.prepare(manifest, stack=_stack(all_merged=False))


def test_broker_writes_version_and_changelog(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    broker = VersionChangelogBroker(fixture_root=root)
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Initial Stage 2 fixture release.",
    )

    broker.prepare(manifest, stack=_stack(all_merged=True))

    pyproject_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.1.0"' in pyproject_text
    assert 'name = "enginery-stage2-fixture"' in pyproject_text
    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog_text.startswith("# Changelog\n")
    assert "## 0.1.0" in changelog_text
    assert "Initial Stage 2 fixture release." in changelog_text


def test_broker_rejects_reused_version(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.1.0\n\nAlready released.\n\n", encoding="utf-8"
    )
    broker = VersionChangelogBroker(fixture_root=root)
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Attempted reuse.",
    )

    with pytest.raises(ExternalConflictError, match="unique"):
        broker.prepare(manifest, stack=_stack(all_merged=True))


def test_broker_rejects_name_mismatch_between_pyproject_and_target(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, name="some-other-package")
    broker = VersionChangelogBroker(fixture_root=root)
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Mismatched name.",
    )

    with pytest.raises(ExternalConflictError, match="does not match"):
        broker.prepare(manifest, stack=_stack(all_merged=True))


def test_broker_requires_absolute_fixture_root() -> None:
    with pytest.raises(InvalidInputError):
        VersionChangelogBroker(fixture_root=Path("relative/path"))


def test_broker_never_writes_the_product_pyproject_even_if_target_name_slips_through(
    tmp_path: Path,
) -> None:
    """Defense in depth: even if a caller bypassed validate_release_target,
    the pyproject name-match check independently refuses a mismatched write."""
    root = _fixture_root(tmp_path, name="enginery")
    broker = VersionChangelogBroker(fixture_root=root)
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Should never write.",
    )

    with pytest.raises(ExternalConflictError):
        broker.prepare(manifest, stack=_stack(all_merged=True))

    assert 'version = "0.0.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_prepare_is_a_no_op_on_disk_when_constituent_merge_check_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    broker = VersionChangelogBroker(fixture_root=root)
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Should never write.",
    )

    with pytest.raises(ExternalConflictError):
        broker.prepare(manifest, stack=_stack(all_merged=False))

    assert not (root / "CHANGELOG.md").exists()
    assert 'version = "0.0.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")
