"""Tests for scripts/verify_project_identity.py."""

from __future__ import annotations

from typing import Any

import pytest

import verify_project_identity as identity_checker


def _base_pyproject() -> dict[str, Any]:
    return {
        "project": {
            "name": identity_checker.EXPECTED_DISTRIBUTION,
            "scripts": {
                identity_checker.EXPECTED_DISTRIBUTION: identity_checker.EXPECTED_CLI_ENTRY_POINT
            },
            "urls": {"Repository": identity_checker.EXPECTED_REPOSITORY_URL},
        },
        "tool": {"enginery": {"product_name": identity_checker.EXPECTED_PRODUCT_NAME}},
    }


def test_repo_pyproject_has_no_identity_drift() -> None:
    pyproject = identity_checker._load_pyproject()
    assert identity_checker.verify(pyproject) == []


def test_verify_accepts_a_matching_pyproject() -> None:
    assert identity_checker.verify(_base_pyproject()) == []


def test_verify_detects_distribution_name_drift() -> None:
    pyproject = _base_pyproject()
    pyproject["project"]["name"] = "wrong-name"

    errors = identity_checker.verify(pyproject)

    assert any("python distribution name" in error for error in errors)


def test_verify_detects_product_name_drift() -> None:
    pyproject = _base_pyproject()
    pyproject["tool"]["enginery"]["product_name"] = "enginery"

    errors = identity_checker.verify(pyproject)

    assert any("product display name" in error for error in errors)


def test_verify_detects_missing_cli_entry_point() -> None:
    pyproject = _base_pyproject()
    pyproject["project"]["scripts"] = {}

    errors = identity_checker.verify(pyproject)

    assert any("CLI entry point" in error for error in errors)


def test_verify_detects_repository_url_drift() -> None:
    pyproject = _base_pyproject()
    pyproject["project"]["urls"]["Repository"] = "https://github.com/someone-else/enginery"

    errors = identity_checker.verify(pyproject)

    assert any("repository URL" in error for error in errors)


def test_verify_detects_missing_import_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_checker, "EXPECTED_IMPORT_PACKAGE", "no_such_package")

    errors = identity_checker.verify(_base_pyproject())

    assert any("import package" in error for error in errors)
