"""Tests for enginery.evaluation.gate_floor: the registered readiness
floor and human-principal roster loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.evaluation.gate_floor import GateFloorConfig, load_gate_floor_config


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reports_every_floor_as_unset_when_config_omits_them(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "floor.toml",
        """
        schema_version = 1
        [registered_principals]
        ids = []
        """,
    )

    config = load_gate_floor_config(path)

    assert config == GateFloorConfig(
        schema_version=1,
        registered_principal_ids=(),
        completed_run_volume_floor=None,
        intervention_volume_floor=None,
        outcome_completeness_floor=None,
    )


def test_load_reads_a_fully_registered_config(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "floor.toml",
        """
        schema_version = 1
        [registered_principals]
        ids = ["operator-a", "operator-b"]
        [completed_runs]
        min_total = 40
        [interventions]
        min_with_reason = 10
        [outcome_completeness]
        floor = 0.8
        """,
    )

    config = load_gate_floor_config(path)

    assert config.registered_principal_ids == ("operator-a", "operator-b")
    assert config.completed_run_volume_floor == 40
    assert config.intervention_volume_floor == 10
    assert config.outcome_completeness_floor == 0.8


def test_load_deduplicates_registered_principal_ids(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "floor.toml",
        """
        schema_version = 1
        [registered_principals]
        ids = ["operator-a", "operator-a"]
        """,
    )

    config = load_gate_floor_config(path)

    assert config.registered_principal_ids == ("operator-a",)


def test_load_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="unable to read"):
        load_gate_floor_config(tmp_path / "missing.toml")


def test_load_rejects_malformed_toml(tmp_path: Path) -> None:
    path = _write(tmp_path / "floor.toml", "not valid toml [[[")

    with pytest.raises(InvalidInputError, match="not valid TOML"):
        load_gate_floor_config(path)


def test_load_rejects_an_unsupported_schema_version(tmp_path: Path) -> None:
    path = _write(tmp_path / "floor.toml", "schema_version = 2\n")

    with pytest.raises(InvalidInputError, match="schema version"):
        load_gate_floor_config(path)


def test_load_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    path = _write(tmp_path / "floor.toml", "schema_version = 1\nunexpected = true\n")

    with pytest.raises(InvalidInputError, match="unrecognized"):
        load_gate_floor_config(path)


def test_load_rejects_a_negative_volume_floor(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "floor.toml",
        "schema_version = 1\n[completed_runs]\nmin_total = -1\n",
    )

    with pytest.raises(InvalidInputError, match="non-negative"):
        load_gate_floor_config(path)


def test_load_rejects_a_completeness_floor_outside_the_unit_interval(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "floor.toml",
        "schema_version = 1\n[outcome_completeness]\nfloor = 1.5\n",
    )

    with pytest.raises(InvalidInputError, match="between 0 and 1"):
        load_gate_floor_config(path)


def test_load_rejects_a_non_string_principal_id(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "floor.toml",
        "schema_version = 1\n[registered_principals]\nids = [1]\n",
    )

    with pytest.raises(InvalidInputError, match="non-blank strings"):
        load_gate_floor_config(path)
