"""Tests for enginery.plans.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.plans.loader import load_plan, parse_plan_toml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plans"


def test_parse_plan_toml_matches_load_plan() -> None:
    text = (FIXTURES / "linear.toml").read_text(encoding="utf-8")
    assert parse_plan_toml(text) == load_plan(FIXTURES / "linear.toml")


def test_parse_plan_toml_rejects_malformed_toml() -> None:
    with pytest.raises(InvalidInputError, match="not valid TOML"):
        parse_plan_toml("this is not [ valid toml")


def test_load_plan_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="could not be read"):
        load_plan(tmp_path / "missing.toml")
