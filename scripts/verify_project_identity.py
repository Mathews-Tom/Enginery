#!/usr/bin/env python3
"""Verify Enginery's project identity has one canonical source and no drift.

``pyproject.toml`` is the single canonical source for the approved product
display name, Python distribution name, import package, CLI entry point,
and repository URL. This script cross-checks each of those five dimensions
against the approved values and fails loudly on any mismatch instead of
letting a second, silently diverging copy of the identity exist elsewhere
in the repository.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

EXPECTED_DISTRIBUTION = "enginery"
EXPECTED_PRODUCT_NAME = "Enginery"
EXPECTED_IMPORT_PACKAGE = "enginery"
EXPECTED_CLI_ENTRY_POINT = "enginery.cli.main:main"
EXPECTED_REPOSITORY_URL = "https://github.com/Mathews-Tom/Enginery"


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _record_mismatch(label: str, actual: object, expected: object, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{label}: expected {expected!r}, found {actual!r}")


def verify(pyproject: dict[str, Any]) -> list[str]:
    """Return a list of drift descriptions; empty means no drift."""
    errors: list[str] = []
    project = pyproject.get("project", {})
    tool_enginery = pyproject.get("tool", {}).get("enginery", {})

    _record_mismatch("python distribution name", project.get("name"), EXPECTED_DISTRIBUTION, errors)
    _record_mismatch(
        "product display name",
        tool_enginery.get("product_name"),
        EXPECTED_PRODUCT_NAME,
        errors,
    )

    scripts = project.get("scripts", {})
    _record_mismatch(
        "CLI entry point",
        scripts.get(EXPECTED_DISTRIBUTION),
        EXPECTED_CLI_ENTRY_POINT,
        errors,
    )

    urls = project.get("urls", {})
    _record_mismatch("repository URL", urls.get("Repository"), EXPECTED_REPOSITORY_URL, errors)

    import_package_init = REPO_ROOT / "src" / EXPECTED_IMPORT_PACKAGE / "__init__.py"
    if not import_package_init.is_file():
        errors.append(
            f"import package: expected {import_package_init.relative_to(REPO_ROOT)} to exist"
        )

    return errors


def main() -> int:
    pyproject = _load_pyproject()
    errors = verify(pyproject)
    if errors:
        for error in errors:
            print(f"IDENTITY DRIFT: {error}", file=sys.stderr)
        return 1
    print("project identity verified: no drift detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
