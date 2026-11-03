"""The committed observer must reject real candidate regressions, not just CLI startup."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fault", ["nonfinite_wait", "argparse_text_error"])
def test_drain_observer_rejects_defective_candidate(tmp_path: Path, fault: str) -> None:
    candidate = tmp_path / "candidate"
    shutil.copytree(_ROOT / "src", candidate / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(_ROOT / "pyproject.toml", candidate / "pyproject.toml")
    checks = candidate / ".enginery/checks"
    checks.mkdir(parents=True)
    observer = checks / "operator_drain.py"
    shutil.copy2(_ROOT / ".enginery/checks/operator_drain.py", observer)
    if fault == "nonfinite_wait":
        implementation = candidate / "src/enginery/engine/operator_control.py"
        source = implementation.read_text()
        implementation.write_text(
            source.replace(
                "if not math.isfinite(wait_seconds) or wait_seconds < 0:",
                "if wait_seconds < 0:",
            )
        )
    else:
        implementation = candidate / "src/enginery/cli/operator.py"
        source = implementation.read_text()
        implementation.write_text(
            source.replace(
                "type=float,",
                'type=lambda value: float(value) if float(value) >= 0 else parser.error("wait"),',
            )
        )

    # Both candidates still start: a --help probe would accept these regressions.
    startup = subprocess.run(
        [
            sys.executable,
            str(candidate / "src/enginery/adapters/oci/assets/checkout_entry.py"),
            "engine",
            "--help",
        ],
        cwd=candidate,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert startup.returncode == 0, startup.stderr
    observed = subprocess.run(
        [sys.executable, str(observer)],
        cwd=candidate,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert observed.returncode == 1, observed.stdout + observed.stderr
