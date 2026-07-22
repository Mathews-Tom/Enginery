"""Smoke tests for the ``enginery gate status`` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginery.cli.main import main


def _write_floor_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_gate_status_reports_fail_closed_against_an_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml",
        """
        schema_version = 1
        [registered_principals]
        ids = []
        """,
    )

    exit_code = main(
        [
            "gate",
            "status",
            "--gate",
            "G4",
            "--database",
            str(database),
            "--floor-config",
            str(floor_config),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == "G4"
    assert payload["overall"] == "fail"
    conditions = {condition["id"]: condition for condition in payload["conditions"]}
    assert conditions["corpus_diversity"]["status"] == "fail"
    assert conditions["registered_human_principals"]["status"] == "fail"
    assert conditions["completed_run_diversity"]["status"] == "unmeasured"
    assert conditions["recurring_evidence_backed_deficiency"]["status"] == "unmeasured"
    # An empty ledger's fail-closed exit code matches doctor's "missing
    # prerequisite" convention -- the gate has not passed.
    assert exit_code != 0


def test_gate_status_rejects_an_unsupported_gate_name(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 1\n[registered_principals]\nids = []\n"
    )

    with pytest.raises(SystemExit):
        main(
            [
                "gate",
                "status",
                "--gate",
                "G7",
                "--database",
                str(database),
                "--floor-config",
                str(floor_config),
            ]
        )


def test_gate_status_prints_human_readable_output_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 1\n[registered_principals]\nids = []\n"
    )

    main(
        [
            "gate",
            "status",
            "--gate",
            "G4",
            "--database",
            str(database),
            "--floor-config",
            str(floor_config),
        ]
    )

    out = capsys.readouterr().out
    assert out.startswith("gate G4: fail\n")
    assert "[fail] corpus_diversity:" in out
    assert "[unmeasured] recurring_evidence_backed_deficiency:" in out
