"""Tests for the fail-closed `check_docs_currency.py` docs-staleness gate.

Imports the script directly (`pythonpath = [".", "scripts"]` in
`pyproject.toml`), matching `tests/system/test_full_system_gate.py`'s and
`tests/system/test_run_stage3_gate.py`'s convention: exercises the real
gate logic, not a reimplementation.

Builds real, disposable throwaway git repositories under `tmp_path` for the
rejection and acceptance fixtures (`git ls-files` requires a real git index
to discover "tracked" docs); also runs the real check directly against this
repository's own current, already-corrected doc tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from check_docs_currency import DocsCurrencyError, run_check

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _init_fixture_repo(tmp_path: Path, *, version: str, docs: dict[str, str]) -> Path:
    """Build a throwaway git repository with a pyproject version and tracked docs."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "enginery"\nversion = "{version}"\n', encoding="utf-8"
    )
    for relative_path, content in docs.items():
        doc_path = repo / relative_path
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(content, encoding="utf-8")

    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return repo


def test_run_check_passes_against_this_repositorys_real_current_docs() -> None:
    """The check must pass, with zero changes, against the real, shipped doc tree."""
    run_check(repo_root=REPO_ROOT)


def test_stale_self_version_declaration_fails_closed(tmp_path: Path) -> None:
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={
            "docs/operations.md": (
                "# Operations\n\nEnginery is `v0.1.0`. Only Stage 1 is supported.\n"
            ),
        },
    )

    with pytest.raises(DocsCurrencyError) as excinfo:
        run_check(repo_root=repo)

    message = str(excinfo.value)
    assert "docs/operations.md:3" in message
    assert "v0.1.0" in message
    assert "v0.3.0" in message


def test_stale_install_instruction_fails_closed(tmp_path: Path) -> None:
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={"docs/operations.md": "`v0.1.0` is published to PyPI. Install with:\n"},
    )

    with pytest.raises(DocsCurrencyError, match=r"v0\.1\.0"):
        run_check(repo_root=repo)


def test_stale_doctor_example_fails_closed(tmp_path: Path) -> None:
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={
            "docs/operations.md": (
                "```text\n[ok] package_metadata: enginery 0.1.0 installed\n```\n"
            )
        },
    )

    with pytest.raises(DocsCurrencyError, match=r"v0\.1\.0"):
        run_check(repo_root=repo)


def test_stale_status_only_header_fails_closed(tmp_path: Path) -> None:
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={"README.md": "## Status\n\n`v0.1.0` (Stage 1 only). More text follows.\n"},
    )

    with pytest.raises(DocsCurrencyError, match=r"v0\.1\.0"):
        run_check(repo_root=repo)


def test_stale_status_phrase_fails_closed(tmp_path: Path) -> None:
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={
            "docs/workflows.md": (
                "- **Status:** Intended architecture and operating examples; "
                "Enginery is not yet implemented.\n"
            )
        },
    )

    with pytest.raises(DocsCurrencyError) as excinfo:
        run_check(repo_root=repo)

    assert "Enginery is not yet implemented" in str(excinfo.value)


def test_matching_current_version_self_declaration_passes(tmp_path: Path) -> None:
    """A self-declaration matching the canonical version is not stale."""
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={"docs/operations.md": "Enginery is `v0.3.0`. Stages 1-3 are supported.\n"},
    )

    run_check(repo_root=repo)


def test_historical_version_mentions_in_non_status_prose_pass(tmp_path: Path) -> None:
    """Prior-version mentions in legitimate historical narrative are not flagged.

    Mirrors this repository's own real, current `README.md`/`RELEASE_NOTES.md`
    phrasing ("Layered on `v0.1.0`'s Stage 1 and `v0.2.0`'s Stage 2...").
    """
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={
            "RELEASE_NOTES.md": (
                "# Enginery `v0.3.0` Release Notes\n\n"
                "This release adds Stage 3 on top of `v0.1.0`'s Stage 1 and "
                "`v0.2.0`'s Stage 2.\n"
            ),
            "docs/DEPENDENCIES.md": (
                "## Version history\n\n"
                "| Version | Change |\n|---|---|\n"
                "| `v0.1.0` | Baseline |\n"
                "| `v0.2.0` | None |\n"
                "| `v0.3.0` | None |\n"
            ),
        },
    )

    run_check(repo_root=repo)


def test_changelog_and_release_evidence_historical_sections_are_excluded(tmp_path: Path) -> None:
    """The two named historical files are excluded even when they contain stale markers."""
    repo = _init_fixture_repo(
        tmp_path,
        version="0.3.0",
        docs={
            "CHANGELOG.md": (
                "## [0.1.0]\n\nEnginery is `v0.1.0`. Enginery is not yet implemented.\n"
            ),
            "docs/RELEASE_EVIDENCE.md": (
                "## `v0.1.0`\n\nEnginery is `v0.1.0`. Enginery is not yet implemented.\n"
            ),
        },
    )

    run_check(repo_root=repo)


def test_missing_canonical_version_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "no-version-repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "enginery"\n', encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    with pytest.raises(DocsCurrencyError, match="version"):
        run_check(repo_root=repo)
