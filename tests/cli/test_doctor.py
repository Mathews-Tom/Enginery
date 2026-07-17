from __future__ import annotations

import json

import pytest

from enginery.cli.doctor import _check_package_metadata, _check_python_version, run_doctor
from enginery.cli.main import main


def test_doctor_reports_ok_checks_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["doctor"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[ok] python_version" in captured.out
    assert "[ok] package_metadata" in captured.out


def test_doctor_json_output_is_parseable_and_complete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["doctor", "--json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} == {
        "python_version",
        "package_metadata",
    }


def test_run_doctor_returns_both_checks() -> None:
    report = run_doctor()

    assert {check.name for check in report.checks} == {"python_version", "package_metadata"}
    assert report.ok is True


def test_check_python_version_flags_unsupported_version() -> None:
    check = _check_python_version(actual=(3, 11, 0))

    assert check.ok is False
    assert "3.11.0" in check.detail


def test_check_python_version_accepts_minimum_supported_version() -> None:
    check = _check_python_version(actual=(3, 12, 0))

    assert check.ok is True


def test_check_package_metadata_flags_missing_distribution() -> None:
    check = _check_package_metadata(distribution="no-such-distribution")

    assert check.ok is False
    assert "not installed" in check.detail


def test_main_without_command_prints_help_and_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()
