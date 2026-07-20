from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.materialize import materialize_capability
from enginery.capabilities.serialization import write_lock
from enginery.cli.main import main
from enginery.domain.digests import Digest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _locked_entry(name: str = "skill-a", payload: bytes = b"payload") -> LockedCapability:
    return LockedCapability(
        name=name,
        version="1",
        digest=Digest.of_bytes(payload),
        provenance=ProvenanceRecord(
            status=ProvenanceStatus.LOCAL_TRUSTED,
            source_label="local",
            signer_key_id=None,
            verified_at=_NOW,
        ),
        license="MIT",
        introduced_by_run=False,
    )


def test_capability_lock_check_without_lockfile_reports_nothing_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "capabilities.lock.json"
    capabilities_root = tmp_path / "capabilities"

    exit_code = main(
        [
            "capability",
            "lock",
            "--check",
            "--lockfile",
            str(lockfile),
            "--capabilities-root",
            str(capabilities_root),
        ]
    )

    assert exit_code == 0
    assert "nothing to check" in capsys.readouterr().out


def test_capability_lock_check_reports_healthy_for_matching_materialized_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "capabilities.lock.json"
    capabilities_root = tmp_path / "capabilities"
    entry = _locked_entry()
    write_lock(CapabilityLock(entries=(entry,)), lockfile)
    materialize_capability(entry, b"payload", root=capabilities_root)

    exit_code = main(
        [
            "capability",
            "lock",
            "--check",
            "--lockfile",
            str(lockfile),
            "--capabilities-root",
            str(capabilities_root),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "OK skill-a@1" in output
    assert "no drift" in output


def test_capability_lock_check_detects_tampered_materialized_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "capabilities.lock.json"
    capabilities_root = tmp_path / "capabilities"
    entry = _locked_entry()
    write_lock(CapabilityLock(entries=(entry,)), lockfile)
    path = materialize_capability(entry, b"payload", root=capabilities_root)
    path.write_bytes(b"tampered-bytes")

    exit_code = main(
        [
            "capability",
            "lock",
            "--check",
            "--lockfile",
            str(lockfile),
            "--capabilities-root",
            str(capabilities_root),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code != 0
    assert "DRIFT skill-a@1" in output
    assert "drift detected" in output


def test_capability_lock_check_detects_missing_materialized_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "capabilities.lock.json"
    capabilities_root = tmp_path / "capabilities"
    entry = _locked_entry()
    write_lock(CapabilityLock(entries=(entry,)), lockfile)

    exit_code = main(
        [
            "capability",
            "lock",
            "--check",
            "--lockfile",
            str(lockfile),
            "--capabilities-root",
            str(capabilities_root),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code != 0
    assert "missing" in output


def test_capability_lock_check_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "capabilities.lock.json"
    capabilities_root = tmp_path / "capabilities"
    entry = _locked_entry()
    write_lock(CapabilityLock(entries=(entry,)), lockfile)
    materialize_capability(entry, b"payload", root=capabilities_root)

    exit_code = main(
        [
            "capability",
            "lock",
            "--check",
            "--lockfile",
            str(lockfile),
            "--capabilities-root",
            str(capabilities_root),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["findings"][0]["name"] == "skill-a"


def test_capability_lock_without_check_flag_errors(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["capability", "lock"])

    assert exit_code != 0
    assert "--check" in capsys.readouterr().err


def test_capability_without_subcommand_errors(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["capability"])

    assert exit_code != 0
    assert "capability subcommand is required" in capsys.readouterr().err
