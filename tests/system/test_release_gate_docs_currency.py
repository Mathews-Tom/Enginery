"""Tests proving `check_docs_currency.py` is wired into `release_gate.py`.

Imports `release_gate` directly (`pythonpath = [".", "scripts"]` in
`pyproject.toml`), matching the existing `tests/system/` convention. Does
not exercise `run_gate`'s real `uv build`/`twine check`/install-smoke path
(covered operationally by each release-preparation pass); isolates the
docs-currency wiring and its fail-fast ordering instead.
"""

from __future__ import annotations

import pytest

import release_gate
from check_docs_currency import DocsCurrencyError
from release_gate import ReleaseGateError


def test_check_docs_currency_passes_for_the_real_repository() -> None:
    """The wiring calls the real check function, not a stand-in; today's repo passes."""
    release_gate._check_docs_currency()


def test_check_docs_currency_wraps_a_docs_currency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*, repo_root: object = None) -> None:
        raise DocsCurrencyError("fixture: stale doc detected")

    monkeypatch.setattr(release_gate, "_run_docs_currency_check", _raise)

    with pytest.raises(ReleaseGateError, match="docs-currency check failed") as excinfo:
        release_gate._check_docs_currency()
    assert "fixture: stale doc detected" in str(excinfo.value)


def test_run_gate_fails_closed_before_building_artifacts_when_docs_are_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docs-currency check runs, and fails the whole gate, before the expensive build."""

    def _raise(*, repo_root: object = None) -> None:
        raise DocsCurrencyError("fixture: stale doc detected")

    def _fail_if_called(expected_version: str) -> tuple[object, object]:
        raise AssertionError("_build_fresh_artifacts must not run when docs currency fails")

    monkeypatch.setattr(release_gate, "_run_docs_currency_check", _raise)
    monkeypatch.setattr(release_gate, "_build_fresh_artifacts", _fail_if_called)

    with pytest.raises(ReleaseGateError, match="docs-currency check failed"):
        release_gate.run_gate(version="0.3.0", skip_install_smoke=True)
