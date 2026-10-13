"""Run a validation command inside the isolated runner instead of on the host.

Validation executes candidate-authored code (tests, conftest, hooks). Run host-side it would
execute with the operator's identity and environment, outside the worker boundary. This runner
gives it exactly the worker's restrictions: the hardened container, the task-local workspace as
its only writable tree, no credentials, and no route to anything but the internal runner network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from enginery.adapters.oci.launch import container_name_for
from enginery.adapters.runner_launch import (
    LAUNCHER_MODULE,
    MAX_RESULT_BYTES,
    read_bounded_text,
    worker_environment,
)
from enginery.domain.errors import WorkerFailureError
from enginery.domain.runner import (
    LaunchSpec,
    PermissionMapping,
    RunnerContext,
)
from enginery.engine.task_repository import out_path_for
from enginery.ledger.redaction import redact_credential_shaped_text

_HARNESS = "validation"
_DEFAULT_TIME_BUDGET_SECONDS = 900
_RESULT_NAME = "result.json"
_EXIT_LAUNCHER_FAULT = 125

LauncherProcess = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run_launcher(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )


def _validation_permissions() -> PermissionMapping:
    """No harness runs: the command is bounded by the container restrictions alone."""
    return PermissionMapping(
        harness=_HARNESS,
        name="validation-container-only",
        arguments=(),
        unconditional_bypass=False,
        rationale=(
            "no harness runs during validation; the command runs under the isolated "
            "container's restrictions with no credentials"
        ),
    )


@dataclass(frozen=True, slots=True)
class OciValidationRunner:
    """A ``(command, workspace) -> CompletedProcess`` that runs the command in the runner."""

    runner: RunnerContext
    time_budget_seconds: int = _DEFAULT_TIME_BUDGET_SECONDS
    launcher: LauncherProcess = field(default=_run_launcher)

    def __call__(
        self, command: tuple[str, ...], workspace_path: Path
    ) -> subprocess.CompletedProcess[str]:
        image = self.runner.selection.image
        if image is None:  # a RunnerContext always pins one; keep the type honest
            raise WorkerFailureError("an isolated validation needs a pinned runner image")
        attempt = uuid.uuid4().hex
        operation_id = f"validate:{attempt}"
        out_path = out_path_for(workspace_path) / f"validation-{attempt}"
        out_path.mkdir(parents=True, exist_ok=False)
        spec = LaunchSpec(
            operation_id=operation_id,
            container_name=container_name_for(operation_id),
            image=image,
            profile=self.runner.profile,
            harness=_HARNESS,
            workspace_path=str(workspace_path),
            out_path=str(out_path),
            receipt_path=f"{self.runner.receipt_root}/{attempt}.validation-receipt.json",
            result_name=_RESULT_NAME,
            time_budget_seconds=self.time_budget_seconds,
            permissions=_validation_permissions(),
            env=worker_environment(()),
            docker_context=self.runner.selection.docker_context,
        )
        completed = self.launcher(
            (
                sys.executable,
                "-m",
                LAUNCHER_MODULE,
                "--spec",
                json.dumps(spec.to_state(), sort_keys=True, separators=(",", ":")),
                "--",
                *command,
            )
        )
        if completed.returncode == _EXIT_LAUNCHER_FAULT:
            # The container never ran the command: a refused launch is not a failed validation.
            raise WorkerFailureError(
                "the isolated runner refused to launch the validation command",
                details={"stderr": completed.stderr.strip()[-2000:]},
            )
        output = _worker_output(out_path / _RESULT_NAME)
        return subprocess.CompletedProcess(
            args=command,
            returncode=completed.returncode,
            stdout=redact_credential_shaped_text(output),
            stderr="",
        )


def _worker_output(result_path: Path) -> str:
    """Read the in-container wrapper's captured output; the file is untrusted worker data."""
    try:
        payload = json.loads(
            read_bounded_text(result_path, limit=MAX_RESULT_BYTES, what="validation result")
        )
    except json.JSONDecodeError as error:
        raise WorkerFailureError("the isolated validation result is not valid JSON") from error
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, str):
        raise WorkerFailureError("the isolated validation result has no output")
    return output


__all__ = ["OciValidationRunner"]
