"""Tests for scripts/check_import_boundaries.py.

Runs the checker against the real source tree for every layer, and against
synthetic fixture trees to prove the checker actually detects a violation
rather than passing vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import check_import_boundaries as boundary_checker

ALL_LAYERS = sorted(boundary_checker.LAYER_ALLOWED_IMPORTS)


@pytest.mark.parametrize("layer", ALL_LAYERS)
def test_current_source_tree_has_no_boundary_violations(layer: str) -> None:
    assert boundary_checker.find_violations(layer) == []


def _write_module(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_domain_module_importing_application_is_flagged(tmp_path: Path) -> None:
    src_root = tmp_path / "enginery"
    _write_module(src_root, "domain/__init__.py", "")
    _write_module(src_root, "domain/bad.py", "from enginery.application import something\n")
    _write_module(src_root, "application/__init__.py", "")

    violations = boundary_checker.find_violations("domain", src_root=src_root)

    assert len(violations) == 1
    assert violations[0].target_layer == "application"


def test_domain_module_importing_domain_is_allowed(tmp_path: Path) -> None:
    src_root = tmp_path / "enginery"
    _write_module(src_root, "domain/__init__.py", "")
    _write_module(src_root, "domain/errors.py", "")
    _write_module(
        src_root, "domain/models.py", "from enginery.domain.errors import EngineryError\n"
    )

    violations = boundary_checker.find_violations("domain", src_root=src_root)

    assert violations == []


def test_relative_import_within_layer_is_allowed(tmp_path: Path) -> None:
    src_root = tmp_path / "enginery"
    _write_module(src_root, "domain/__init__.py", "")
    _write_module(src_root, "domain/errors.py", "")
    _write_module(src_root, "domain/models.py", "from . import errors\n")

    violations = boundary_checker.find_violations("domain", src_root=src_root)

    assert violations == []


def test_relative_import_crossing_layers_is_flagged(tmp_path: Path) -> None:
    src_root = tmp_path / "enginery"
    _write_module(src_root, "domain/__init__.py", "")
    _write_module(src_root, "application/__init__.py", "")
    _write_module(src_root, "domain/models.py", "from .. import application\n")

    violations = boundary_checker.find_violations("domain", src_root=src_root)

    assert len(violations) == 1
    assert violations[0].target_layer == "application"


def test_unknown_layer_directory_raises(tmp_path: Path) -> None:
    src_root = tmp_path / "enginery"
    src_root.mkdir()

    with pytest.raises(SystemExit):
        boundary_checker.find_violations("domain", src_root=src_root)
