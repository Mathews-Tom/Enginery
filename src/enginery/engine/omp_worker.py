"""Execute OMP as a supervised child and atomically retain redacted output."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from enginery.domain.errors import InvalidInputError
from enginery.ledger.redaction import redact_credential_shaped_text

_RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class OmpWorkerResult:
    """The redacted, durable handoff from one supervised OMP process."""

    operation_id: str
    terminal_status: str
    output: str
    schema_version: int = _RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise InvalidInputError("OMP worker operation id must be non-blank")
        if self.terminal_status not in {"succeeded", "failed"}:
            raise InvalidInputError("OMP worker terminal status is unsupported")


def run_omp_worker(
    *,
    operation_id: str,
    command: Sequence[str],
    output_path: Path,
) -> int:
    """Run OMP, redact its combined output, and atomically write its result."""
    if not command or any(not value for value in command):
        raise InvalidInputError("OMP worker command must contain non-blank arguments")
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as error:
        output = redact_credential_shaped_text(str(error))
        result = OmpWorkerResult(operation_id=operation_id, terminal_status="failed", output=output)
        _write_result(output_path, result)
        return 1

    output = redact_credential_shaped_text(completed.stdout or "")
    result = OmpWorkerResult(
        operation_id=operation_id,
        terminal_status="succeeded" if completed.returncode == 0 else "failed",
        output=output,
    )
    _write_result(output_path, result)
    return completed.returncode if completed.returncode >= 0 else 128 + abs(completed.returncode)


def _write_result(output_path: Path, result: OmpWorkerResult) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(result), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    if parsed.command[:1] == ["--"]:
        parsed.command = parsed.command[1:]
    if not parsed.command:
        parser.error("OMP command is required after '--'")
    return parsed


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the worker entrypoint."""
    parsed = _parse_arguments(sys.argv[1:] if arguments is None else arguments)
    return run_omp_worker(
        operation_id=parsed.operation_id,
        command=parsed.command,
        output_path=parsed.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
