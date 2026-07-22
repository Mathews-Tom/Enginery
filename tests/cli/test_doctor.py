from __future__ import annotations

import json
from pathlib import Path

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


def test_adapter_doctor_reports_all_local_provider_kinds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["adapter", "doctor", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {provider["kind"] for provider in payload} == {
        "capability_source",
        "deployment",
        "harness",
        "release",
        "source_control",
        "validation",
        "work_ledger",
        "workspace",
    }
    assert all(provider["availability"] == "available" for provider in payload)


def test_adapter_doctor_reports_stage2_and_stage3_broker_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_script = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "enginery-stage3-local-service"
        / "app.py"
    )

    exit_code = main(
        [
            "adapter",
            "doctor",
            "--json",
            "--github-repository",
            "owner/repo",
            "--github-executable",
            "true",
            "--pypi-project-name",
            "fixture-project",
            "--pypi-executable",
            "true",
            "--deployment-app-script",
            str(app_script),
            "--deployment-artifacts-root",
            str(tmp_path / "artifacts"),
            "--deployment-state-root",
            str(tmp_path / "state"),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 11
    provider_ids = {entry.get("provider_id") for entry in payload}
    assert {"github-release", "pypi", "local-service-deployment"} <= provider_ids
    assert all(entry["availability"] == "available" for entry in payload)


def test_adapter_doctor_reports_misconfigured_deployment_without_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "adapter",
            "doctor",
            "--json",
            "--github-executable",
            "true",
            "--pypi-executable",
            "true",
            "--deployment-app-script",
            str(tmp_path / "no-such-app.py"),
        ]
    )

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    deployment_entries = [entry for entry in payload if entry["kind"] == "deployment"]
    assert any(entry["availability"] == "misconfigured" for entry in deployment_entries)
    misconfigured = next(
        entry for entry in deployment_entries if entry["availability"] == "misconfigured"
    )
    assert "app_script" in misconfigured["detail"]


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
