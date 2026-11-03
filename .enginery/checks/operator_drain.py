"""Exercise drain input through the actual candidate CLI and operation, offline.

Only disposable current-schema ledgers are opened; no workers or providers are launched.
Run from an Enginery checkout with Python 3.12 and its runtime dependencies.
"""

# ruff: noqa: E402 -- candidate metadata and import path must precede candidate imports.
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
_metadata = tempfile.TemporaryDirectory(prefix="enginery-drain-metadata-")
_project = tomllib.loads((CHECKOUT / "pyproject.toml").read_text())["project"]
_dist_info = Path(_metadata.name) / "enginery.dist-info"
_dist_info.mkdir()
_dist_info.joinpath("METADATA").write_text(
    f"Metadata-Version: 2.1\nName: {_project['name']}\nVersion: {_project['version']}\n",
    encoding="utf-8",
)
sys.path[:0] = [str(CHECKOUT / "src"), _metadata.name]

from enginery.domain.errors import InvalidInputError
from enginery.engine.operator_control import drain_wait, read_control_state
from enginery.ledger.service import LedgerService

_ENTRY = CHECKOUT / "src/enginery/adapters/oci/assets/checkout_entry.py"
_INVALID = ("nan", "inf", "-inf", "-1")


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


def logical_ledger(database: Path) -> tuple[str, ...]:
    """Compare every logical table, including projections, not SQLite WAL sidecars."""
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.iterdump())


def invoke(database: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_ENTRY), "operator", "drain", "--database", str(database), *arguments],
        cwd=CHECKOUT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def run() -> int:
    case = "fixture"
    try:
        with tempfile.TemporaryDirectory(prefix="enginery-operator-drain-") as directory:
            root = Path(directory)
            for index, value in enumerate(_INVALID):
                case = f"CLI {value}"
                database = root / f"cli-{index}.db"
                with LedgerService.open(database) as ledger:
                    before = logical_ledger(database)
                    result = invoke(database, f"--wait-seconds={value}")
                    require(result.returncode == 2, f"exit {result.returncode}: {result.stderr}")
                    require(result.stdout == "", f"unexpected stdout: {result.stdout}")
                    error = json.loads(result.stderr)["error"]
                    require(error["failure_class"] == "invalid_input", f"wrong error: {error}")
                    require("finite non-negative" in error["message"], f"unclear error: {error}")
                    require(logical_ledger(database) == before, "rejection mutated the ledger")
                    require(not read_control_state(ledger).paused, "rejection paused dispatch")
            print("PASS CLI numeric rejection and JSON without mutation")
            for index, value in enumerate(_INVALID):
                case = f"operation {value}"
                database = root / f"api-{index}.db"
                with LedgerService.open(database) as ledger:
                    before = logical_ledger(database)
                    try:
                        drain_wait(ledger, now=datetime.now(UTC), wait_seconds=float(value))
                    except InvalidInputError:
                        pass
                    else:
                        raise AssertionError("operation accepted an invalid wait")
                    require(logical_ledger(database) == before, "operation mutated the ledger")
                    require(not read_control_state(ledger).paused, "operation paused dispatch")
            print("PASS operation rejection without mutation")
            for index, arguments in enumerate(
                (("--wait-seconds=0",), ("--wait-seconds=0.01",), ("--stop",))
            ):
                case = f"valid {arguments[0]}"
                database = root / f"valid-{index}.db"
                with LedgerService.open(database) as ledger:
                    result = invoke(database, *arguments)
                    require(result.returncode == 0, f"exit {result.returncode}: {result.stderr}")
                    require(result.stderr == "", f"unexpected stderr: {result.stderr}")
                    report = json.loads(result.stdout)
                    require(report["paused"] is True, "valid drain did not pause")
                    require(report["drained"] is True, "idle ledger did not drain")
                    require(report["remaining"] == [], "idle ledger has remaining workers")
                    require(read_control_state(ledger).paused, "pause was not durable")
            print("PASS valid zero, positive and stop")
        print("operator-drain: ok")
        return 0
    except Exception as error:
        print(f"FAIL {case}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
