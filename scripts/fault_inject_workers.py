#!/usr/bin/env python3
"""Run platform-specific worker recovery fault scenarios."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from enginery.engine.recovery import assess_workspace_quiescence
from fault_injection.framework import FaultScenario, main_for


def _workspace_lock_blocks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True, text=True)
        lock = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--git-path", "index.lock"],
            check=True,
            capture_output=True,
            text=True,
        )
        lock_path = Path(lock.stdout.strip())
        if not lock_path.is_absolute():
            lock_path = workspace / lock_path
        lock_path.touch()
        result = assess_workspace_quiescence(workspace)
        if result.ready_to_release or result.reason != "workspace_git_lock_present":
            raise AssertionError("Git lock did not block automatic recovery")


def _platform_supported() -> None:
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise AssertionError(f"unsupported platform {sys.platform}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True)
    parser.parse_args()
    return main_for(
        (
            FaultScenario(
                "platform_identity",
                "current platform has a supported process identity probe",
                _platform_supported,
            ),
            FaultScenario(
                "workspace_lock",
                "Git index lock blocks automatic recovery",
                _workspace_lock_blocks,
            ),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
