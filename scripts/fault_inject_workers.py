#!/usr/bin/env python3
"""Run platform-specific worker recovery fault scenarios."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from enginery.engine.recovery import assess_workspace_quiescence
from fault_injection.framework import FaultScenario, main_for


def _workspace_lock_blocks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        result = assess_workspace_quiescence(workspace)
        if result.ready_to_release:
            raise AssertionError("non-Git workspace was incorrectly considered quiescent")


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
                "ambiguous_workspace",
                "uninspectable workspace blocks recovery",
                _workspace_lock_blocks,
            ),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
