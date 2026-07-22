#!/usr/bin/env python3
"""Fail-closed check that tracked documentation describes the current release.

Guards against a defect this repository has shipped twice: a tracked doc
declaring Enginery's own version as an already-superseded release, or
describing a shipped capability as an unimplemented product concept, after
a later version actually published. Both defects are named directly in the
`v0.3.0` doc-sync correction (`README.md` and `docs/operations.md` still
read "Enginery is `v0.1.0`" after `v0.2.0` and `v0.3.0` had both already
published; `docs/overview.md`, `docs/pitch.md`, and `docs/workflows.md`
still called Enginery an unimplemented "product concept" after Stages 1-3
had shipped for real).

Two independent, deliberately narrow checks:

1. Self-version-declaration patterns (`STALE_SELF_VERSION_PATTERNS`) --
   sentence forms that assert Enginery's *own current* version. A match
   whose captured version differs from the canonical `pyproject.toml`
   version is a stale self-declaration.
2. Stale-status phrases (`STALE_STATUS_PHRASES`) -- literal strings that
   describe Enginery as an unimplemented product concept, proven stale by
   the same correction.

Both lists are deliberately curated, not a generic "contains an old
version number" or "contains 'not yet implemented'" substring match.
Version numbers from earlier releases appear correctly and constantly
throughout the current, already-accurate doc set (for example, "Layered
on `v0.1.0`'s Stage 1 and `v0.2.0`'s Stage 2..."), and "not yet
implemented" is the true, correct description of Stage 4 today. A broad
match would fail closed against legitimate, current text; a narrow,
evidence-grounded list does not. Extend either list when a new
self-declaration or status-framing convention is introduced and found
stale in review.

`CHANGELOG.md` and `docs/RELEASE_EVIDENCE.md` are excluded entirely: both
are append-only, newest-first historical records where every past
version's own accurate-at-the-time status is expected content, not a
regression.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Entire files excluded from both checks below: append-only, newest-first
# historical records. Every past release's own contemporaneous version and
# status language is expected, correct content here, not staleness.
EXCLUDED_DOCS = frozenset({"CHANGELOG.md", "docs/RELEASE_EVIDENCE.md"})

# Sentence forms that assert Enginery's *own current* version. Each pattern
# captures exactly one version number; a captured version that does not
# equal the canonical `pyproject.toml` version is a stale self-declaration.
# Grounded in the two concrete defects the `v0.3.0` doc-sync correction
# fixed in `README.md` and `docs/operations.md`.
STALE_SELF_VERSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Enginery is `v(\d+\.\d+\.\d+)`"),
    re.compile(r"`v(\d+\.\d+\.\d+)` is published to PyPI"),
    re.compile(r"package_metadata: enginery (\d+\.\d+\.\d+) installed"),
    re.compile(r"`v(\d+\.\d+\.\d+)` \(Stage \d+ only\)"),
)

# Literal phrases describing Enginery as an unimplemented product concept.
# Every phrase below was live in this repository's tracked docs until the
# `v0.3.0` doc-sync correction removed it, well after the described
# capability had actually shipped.
STALE_STATUS_PHRASES: tuple[str, ...] = (
    "Product concept and architecture reference",
    "Product concept; no productivity, market-size, or security outcome is claimed",
    "Enginery is not yet implemented",
    "design targets, not demonstrated product capabilities",
    "Enginery remains a product concept until each target has produced its stated evidence",
    "All Enginery contributions in this table are intended behavior, not implemented capability",
)


class DocsCurrencyError(RuntimeError):
    """A fatal, fail-closed docs-currency check failure."""


def _canonical_version(repo_root: Path) -> str:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise DocsCurrencyError("pyproject.toml has no [project] table")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise DocsCurrencyError("pyproject.toml has no [project].version")
    return version


def _tracked_markdown_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DocsCurrencyError(f"'git ls-files' failed:\n{result.stdout}\n{result.stderr}")
    names = [name for name in result.stdout.split("\0") if name]
    return [repo_root / name for name in names if name not in EXCLUDED_DOCS]


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _check_self_version_declarations(
    files: list[Path], repo_root: Path, canonical_version: str
) -> list[str]:
    failures: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in STALE_SELF_VERSION_PATTERNS:
            for match in pattern.finditer(text):
                declared = match.group(1)
                if declared == canonical_version:
                    continue
                line = _line_number(text, match.start())
                failures.append(
                    f"{path.relative_to(repo_root)}:{line}: declares Enginery's own version as "
                    f"`v{declared}` ({match.group(0)!r}), but the canonical version is "
                    f"`v{canonical_version}`"
                )
    return failures


def _check_stale_status_phrases(files: list[Path], repo_root: Path) -> list[str]:
    failures: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for phrase in STALE_STATUS_PHRASES:
            index = text.find(phrase)
            if index == -1:
                continue
            line = _line_number(text, index)
            failures.append(f"{path.relative_to(repo_root)}:{line}: stale status phrase {phrase!r}")
    return failures


def run_check(*, repo_root: Path = REPO_ROOT) -> None:
    """Run the docs-currency check, raising on the first collected failure set."""
    canonical_version = _canonical_version(repo_root)
    files = _tracked_markdown_files(repo_root)
    failures = _check_self_version_declarations(files, repo_root, canonical_version)
    failures += _check_stale_status_phrases(files, repo_root)
    if failures:
        joined = "\n".join(f"  - {failure}" for failure in failures)
        raise DocsCurrencyError(f"docs-currency check failed:\n{joined}")


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        run_check()
    except DocsCurrencyError as error:
        print(f"DOCS CURRENCY CHECK FAILED: {error}", file=sys.stderr)
        return 1
    print("PASS docs-currency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
