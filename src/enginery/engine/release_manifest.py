"""Release-target validation and the version/changelog broker for one fixture release.

A "fixed broker" here means a plain, product-owned Python function that
performs the actual mutation (rewriting a version field and prepending a
changelog entry) directly against files -- never by constructing and
running an agent-authored shell command. The broker itself has no policy
authority: the caller evaluates ``release.prepare`` through
``PolicyEvaluator`` before invoking it, exactly like every other
side-effecting action in this codebase, and this module never reads or
writes any credential.

Every write here is confined to a caller-supplied fixture root, and this
module refuses outright to touch a distribution named after the product
itself. Stage 2's own fixture publication must never consume the
product's name or version.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.domain.stack import Stack, StackSliceState

_VERSION_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")
_RESERVED_DISTRIBUTION_NAMES = frozenset({"enginery"})
_CHANGELOG_HEADER = "# Changelog\n"
_CHANGELOG_VERSION_RE = re.compile(r"^## (?P<version>\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    """A validated fixture release identity: distinct from the product's own."""

    distribution_name: str
    version: str

    def __post_init__(self) -> None:
        if not self.distribution_name.strip():
            raise InvalidInputError("distribution_name must be non-blank")
        if not _VERSION_RE.match(self.version):
            raise InvalidInputError(
                "version must be a plain MAJOR.MINOR.PATCH version, valid under both "
                "semver and PEP 440 (no pre-release or build metadata) -- a Python "
                "wheel version cannot safely round-trip semver pre-release syntax",
                details={"version": self.version},
            )


def validate_release_target(target: ReleaseTarget, *, known_versions: frozenset[str]) -> None:
    """Refuse a fixture release that could ever be mistaken for the product's own.

    Hard-fails, never warns, on a name collision with the product or a
    version already recorded for this fixture -- PyPI version
    immutability means silent reuse is a correctness bug, not a
    warning-level concern.
    """
    if target.distribution_name.strip().lower() in _RESERVED_DISTRIBUTION_NAMES:
        raise ExternalConflictError(
            "a Stage 2 fixture release must never use the product's own distribution name",
            details={"distribution_name": target.distribution_name},
        )
    if target.version in known_versions:
        raise ExternalConflictError(
            "a fixture release version must be unique; it was already recorded",
            details={"version": target.version},
        )


def constituent_work_merged(stack: Stack) -> bool:
    """Whether every constituent milestone in the stack has actually merged."""
    return all(slice_.state is StackSliceState.MERGED for slice_ in stack.ordered_slices)


def known_versions_from_changelog(path: Path) -> frozenset[str]:
    """Every version already recorded in a fixture's own changelog."""
    if not path.exists():
        return frozenset()
    return frozenset(_CHANGELOG_VERSION_RE.findall(path.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """The prepared, not-yet-published release identity and changelog entry."""

    target: ReleaseTarget
    changelog_entry: str

    def __post_init__(self) -> None:
        if not self.changelog_entry.strip():
            raise InvalidInputError("changelog_entry must be non-blank")


@dataclass(frozen=True, slots=True)
class VersionChangelogBroker:
    """Writes one fixture package's version and changelog files directly.

    Confined to ``fixture_root`` -- never the product's own
    ``pyproject.toml`` or ``CHANGELOG.md``. Refuses to run unless every
    constituent milestone in the supplied stack has already merged,
    matching "version/changelog preparation cannot begin before
    implementation gates pass."
    """

    fixture_root: Path

    def __post_init__(self) -> None:
        if not self.fixture_root.is_absolute():
            raise InvalidInputError("fixture_root must be an absolute path")

    def prepare(self, manifest: ReleaseManifest, *, stack: Stack) -> ReleaseManifest:
        if not constituent_work_merged(stack):
            raise ExternalConflictError(
                "release preparation cannot begin before every constituent "
                "milestone in the stack has merged"
            )
        known_versions = known_versions_from_changelog(self.fixture_root / "CHANGELOG.md")
        validate_release_target(manifest.target, known_versions=known_versions)
        self._rewrite_pyproject_version(manifest.target)
        self._prepend_changelog_entry(manifest.target.version, manifest.changelog_entry)
        return manifest

    def _rewrite_pyproject_version(self, target: ReleaseTarget) -> None:
        path = self.fixture_root / "pyproject.toml"
        original_text = path.read_text(encoding="utf-8")
        original = tomllib.loads(original_text)
        project = original.get("project")
        if not isinstance(project, dict) or project.get("name") != target.distribution_name:
            raise ExternalConflictError(
                "fixture pyproject.toml project name does not match the release target",
                details={
                    "expected": target.distribution_name,
                    "found": (project or {}).get("name"),
                },
            )
        replaced = False
        new_lines: list[str] = []
        in_project_table = False
        for line in original_text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("["):
                in_project_table = stripped == "[project]"
            if (
                in_project_table
                and not replaced
                and stripped.startswith("version")
                and "=" in stripped
            ):
                new_lines.append(f'version = "{target.version}"\n')
                replaced = True
                continue
            new_lines.append(line)
        if not replaced:
            raise InternalInvariantViolationError(
                "fixture pyproject.toml has no [project] version field to update"
            )
        new_text = "".join(new_lines)
        updated = tomllib.loads(new_text)
        if updated.get("project", {}).get("version") != target.version:
            raise InternalInvariantViolationError("version rewrite did not take effect as expected")
        path.write_text(new_text, encoding="utf-8")

    def _prepend_changelog_entry(self, version: str, entry: str) -> None:
        path = self.fixture_root / "CHANGELOG.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else _CHANGELOG_HEADER + "\n"
        if not existing.startswith(_CHANGELOG_HEADER):
            raise InternalInvariantViolationError(
                "fixture CHANGELOG.md must start with '# Changelog'"
            )
        marker = f"## {version}\n"
        if marker in existing:
            raise ExternalConflictError(
                "changelog already records this version", details={"version": version}
            )
        block = f"{marker}\n{entry.strip()}\n\n"
        rest = existing[len(_CHANGELOG_HEADER) :].lstrip("\n")
        path.write_text(f"{_CHANGELOG_HEADER}\n{block}{rest}", encoding="utf-8")


__all__ = [
    "ReleaseManifest",
    "ReleaseTarget",
    "VersionChangelogBroker",
    "constituent_work_merged",
    "known_versions_from_changelog",
    "validate_release_target",
]
