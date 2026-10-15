from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginery.capabilities.materialize import materialize_capability
from enginery.capabilities.serialization import write_lock
from enginery.cli.main import main
from enginery.domain.digests import Digest
from tests.support.capabilities import NOW, lock_of, locked_entry, package_files


def _check(lockfile: Path, capabilities_root: Path, *extra: str) -> int:
    return main(
        [
            "capability",
            "lock",
            "--check",
            "--lockfile",
            str(lockfile),
            "--capabilities-root",
            str(capabilities_root),
            *extra,
        ]
    )


def _materialized(tmp_path: Path) -> tuple[Path, Path, Path]:
    files = package_files("skill-a", extra={"scripts/run.py": b"print('ok')\n"})
    entry = locked_entry(files=files)
    lockfile = tmp_path / "capabilities.lock.json"
    capabilities_root = tmp_path / "capabilities"
    write_lock(lock_of(entry), lockfile)
    directory = materialize_capability(entry, files, root=capabilities_root)
    return lockfile, capabilities_root, directory


def test_capability_lock_check_without_lockfile_reports_nothing_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _check(tmp_path / "capabilities.lock.json", tmp_path / "capabilities")

    assert exit_code == 0
    assert "nothing to check" in capsys.readouterr().out


def test_capability_lock_check_reports_healthy_for_a_matching_materialized_closure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile, capabilities_root, _ = _materialized(tmp_path)

    exit_code = _check(lockfile, capabilities_root)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "OK skill-a@1" in output
    assert "no drift" in output


def test_capability_lock_check_detects_a_tampered_sibling_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile, capabilities_root, directory = _materialized(tmp_path)
    script = directory / "scripts" / "run.py"
    script.chmod(0o644)
    script.write_bytes(b"print('tampered')\n")

    exit_code = _check(lockfile, capabilities_root)

    output = capsys.readouterr().out
    assert exit_code != 0
    assert "DRIFT skill-a@1" in output
    assert "changed: scripts/run.py" in output
    assert "drift detected" in output


def test_capability_lock_check_detects_an_added_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile, capabilities_root, directory = _materialized(tmp_path)
    (directory / "references" / "smuggled.md").write_text("extra", encoding="utf-8")

    exit_code = _check(lockfile, capabilities_root)

    assert exit_code != 0
    assert "unexpected: references/smuggled.md" in capsys.readouterr().out


def test_capability_lock_check_detects_a_symlink_swapped_into_the_closure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile, capabilities_root, directory = _materialized(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (directory / "references" / "guide.md").unlink()
    (directory / "references" / "guide.md").symlink_to(outside)

    exit_code = _check(lockfile, capabilities_root)

    assert exit_code != 0
    assert "DRIFT skill-a@1" in capsys.readouterr().out


def test_capability_lock_check_detects_missing_materialized_closure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "capabilities.lock.json"
    write_lock(lock_of(locked_entry()), lockfile)

    exit_code = _check(lockfile, tmp_path / "capabilities")

    output = capsys.readouterr().out
    assert exit_code != 0
    assert "missing" in output


def test_capability_lock_check_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile, capabilities_root, _ = _materialized(tmp_path)

    exit_code = _check(lockfile, capabilities_root, "--json")

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["findings"][0]["name"] == "skill-a"


def _write_legacy_lock(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "name": "skill-a",
                        "version": "1",
                        "digest": str(Digest.of_bytes(b"blob")),
                        "license": "MIT",
                        "introduced_by_run": False,
                        "provenance": {
                            "status": "local_trusted",
                            "source_label": "local",
                            "signer_key_id": None,
                            "verified_at": NOW.isoformat(),
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_capability_lock_check_reports_a_legacy_lock_as_needing_re_resolution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = tmp_path / "legacy.lock.json"
    _write_legacy_lock(lockfile)

    exit_code = _check(lockfile, tmp_path / "capabilities", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert payload["ok"] is False
    assert "re-resolve" in payload["findings"][0]["detail"]


def test_capability_lock_without_check_flag_errors(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["capability", "lock"])

    assert exit_code != 0
    assert "--check" in capsys.readouterr().err


def test_capability_without_subcommand_errors(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["capability"])

    assert exit_code != 0
    assert "capability subcommand is required" in capsys.readouterr().err
