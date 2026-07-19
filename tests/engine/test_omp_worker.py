from __future__ import annotations

import json
import sys
from pathlib import Path

from enginery.engine.omp_worker import run_omp_worker


def _result(output_path: Path) -> dict[str, object]:
    return json.loads(output_path.read_text(encoding="utf-8"))


def test_run_omp_worker_retains_redacted_success_output(tmp_path: Path) -> None:
    output_path = tmp_path / "result.json"

    exit_code = run_omp_worker(
        operation_id="operation-1",
        command=(
            sys.executable,
            "-c",
            "print('token=abcdefghijklmnop'); print('agent_end')",
        ),
        output_path=output_path,
    )

    assert exit_code == 0
    assert _result(output_path) == {
        "operation_id": "operation-1",
        "output": "[REDACTED:generic_secret_assignment]\nagent_end\n",
        "schema_version": 1,
        "terminal_status": "succeeded",
    }


def test_run_omp_worker_retains_redacted_failure_output(tmp_path: Path) -> None:
    output_path = tmp_path / "result.json"

    exit_code = run_omp_worker(
        operation_id="operation-1",
        command=(
            sys.executable,
            "-c",
            "import sys; print('token=abcdefghijklmnop'); sys.exit(7)",
        ),
        output_path=output_path,
    )

    assert exit_code == 7
    assert _result(output_path)["terminal_status"] == "failed"
    assert _result(output_path)["output"] == "[REDACTED:generic_secret_assignment]\n"


def test_run_omp_worker_normalizes_signal_exit_status(tmp_path: Path) -> None:
    output_path = tmp_path / "result.json"

    exit_code = run_omp_worker(
        operation_id="operation-1",
        command=(
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ),
        output_path=output_path,
    )

    assert exit_code == 143
    assert _result(output_path)["terminal_status"] == "failed"


def test_run_omp_worker_records_unavailable_command(tmp_path: Path) -> None:
    output_path = tmp_path / "result.json"

    exit_code = run_omp_worker(
        operation_id="operation-1",
        command=("enginery-command-that-does-not-exist",),
        output_path=output_path,
    )

    assert exit_code == 1
    assert _result(output_path)["terminal_status"] == "failed"
