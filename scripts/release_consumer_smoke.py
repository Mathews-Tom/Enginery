#!/usr/bin/env python3
"""Exercise the released G4 command surface from an isolated installation."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


class ConsumerSmokeError(RuntimeError):
    """Raised when a released consumer command does not meet its contract."""


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _require_success(command: list[str], *, cwd: Path) -> None:
    result = _run(command, cwd=cwd)
    if result.returncode != 0:
        raise ConsumerSmokeError(
            f"command failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}"
        )


def _require_g4_fail_closed(command: list[str], *, cwd: Path) -> None:
    result = _run(command, cwd=cwd)
    if result.returncode != 3 or '"overall": "fail"' not in result.stdout:
        raise ConsumerSmokeError(f"G4 did not remain fail-closed: {result.stdout}\n{result.stderr}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wheel", type=Path)
    source.add_argument(
        "--package", help="published distribution requirement, for example enginery==0.5.0"
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="enginery-consumer-") as temporary_directory:
        root = Path(temporary_directory)
        venv = root / "venv"
        _require_success([sys.executable, "-m", "venv", str(venv)], cwd=root)
        executable = venv / "bin" / "enginery"
        install = ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), "--no-cache"]
        install.append(str(args.wheel.resolve()) if args.wheel is not None else args.package)
        _require_success(install, cwd=root)
        database = root / "ledger.db"
        _require_success([str(executable), "gate", "record-g4-deficiency", "--help"], cwd=root)
        _require_success(
            [str(executable), "gate", "record-g4-deficiency-evidence", "--help"], cwd=root
        )
        _require_success([str(executable), "stage1", "build-request", "--help"], cwd=root)
        _require_g4_fail_closed(
            [
                str(executable),
                "gate",
                "status",
                "--gate",
                "G4",
                "--database",
                str(database),
                "--json",
            ],
            cwd=root,
        )
    print("PASS release-consumer-smoke")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConsumerSmokeError as error:
        print(f"RELEASE-CONSUMER SMOKE FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
