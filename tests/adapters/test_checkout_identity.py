from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from enginery.adapters.checkout_identity import verified_build
from enginery.domain.errors import StaleEvidenceError


def _git(path: Path, *argv: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *argv), check=True, capture_output=True, text=True
    ).stdout.strip()


def test_build_identity_verifies_actual_current_head_bytes(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    source = tmp_path / "app.py"
    source.write_text("print('correct')\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "initial")
    head = _git(tmp_path, "rev-parse", "HEAD")
    original = verified_build(tmp_path, head)
    source.write_text("print('constant')\n")
    with pytest.raises(
        StaleEvidenceError, match="behavioral_build_mismatch: checkout differs from bound tree"
    ):
        verified_build(tmp_path, head)
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "changed")
    with pytest.raises(StaleEvidenceError, match="HEAD"):
        verified_build(tmp_path, head)
    current = verified_build(tmp_path, _git(tmp_path, "rev-parse", "HEAD"))
    assert current.digest != original.digest
