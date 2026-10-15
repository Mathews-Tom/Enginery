"""Run a validation command inside the isolated runner instead of on the host.

Validation executes candidate-authored code (tests, conftest, hooks). Run host-side it would
execute with the operator's identity and environment, outside the worker boundary. This runner
gives it exactly the worker's restrictions: the hardened container, the task-local workspace as
its only writable tree, no credentials, and no network at all (``--network none``): it shares
no network with the relay or with any other container, so candidate code cannot spend the
operator's model subscription or reach another attempt.

The launcher writes a receipt for every validation container. The runner reads it back, verifies
that it describes this run (operation, image, profile, no network, no profile violation), retains
it in the artifact store and returns it with the command's result, so the validation result
names the receipt it ran under.
"""

from __future__ import annotations

import json
import os
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
    bind_result_to_attempt,
    publish_receipt,
    read_bounded_text,
    read_launch_outcome,
    read_runner_receipt,
    require_exited,
    worker_environment,
)
from enginery.application.runner_ports import AttestedProcess
from enginery.application.worker_environment import LAUNCHER_KEYS, build_worker_environment
from enginery.domain.errors import WorkerFailureError
from enginery.domain.runner import (
    NO_NETWORK,
    LaunchSpec,
    PermissionMapping,
    RunnerContext,
    RunnerReceipt,
    TrustMode,
    profile_violations,
)
from enginery.engine.task_repository import out_path_for
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.redaction import redact_credential_shaped_text

_HARNESS = "validation"
_DEFAULT_TIME_BUDGET_SECONDS = 900
_RESULT_NAME = "result.json"


LauncherProcess = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run_launcher(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the launcher with only what it needs to reach the engine, not the ambient environment.

    The container's own environment is the spec's explicit one; the launcher process itself holds
    no source-host, factory, broker or cloud credential either.
    """
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        env=build_worker_environment(os.environ, LAUNCHER_KEYS),
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


def validation_receipt_mismatches(
    receipt: RunnerReceipt, runner: RunnerContext, operation_id: str
) -> tuple[str, ...]:
    """Every way ``receipt`` does not describe this validation container under the profile.

    The container must belong to this operation, run the pinned image under the admitted
    profile with the validation permission mapping, have been attached to no network at all, and
    satisfy every profile check (non-root, no capabilities, read-only root, limits, only the
    two admitted mounts, no environment outside the allowlist).
    """
    checks: tuple[tuple[str, bool], ...] = (
        ("operation", receipt.operation_id == operation_id),
        ("trust_mode", receipt.trust_mode is TrustMode.ISOLATED_OCI),
        ("profile", receipt.profile_digest == runner.profile.digest),
        ("image", receipt.image.image_id == runner.selection.image),
        ("container", receipt.container_name == container_name_for(operation_id)),
        ("permissions", receipt.permissions == _validation_permissions()),
    )
    problems = [name for name, ok in checks if not ok]
    problems += [
        f"effective: {violation}"
        for violation in profile_violations(runner.profile, receipt.effective, network=NO_NETWORK)
    ]
    return tuple(problems)


@dataclass(frozen=True, slots=True)
class OciValidationRunner:
    """A ``(command, workspace) -> AttestedProcess`` that runs the command in the runner."""

    runner: RunnerContext
    artifact_root: Path
    """The artifact store the verified validation receipts are retained in."""
    time_budget_seconds: int = _DEFAULT_TIME_BUDGET_SECONDS
    launcher: LauncherProcess = field(default=_run_launcher)

    def __call__(self, command: tuple[str, ...], workspace_path: Path) -> AttestedProcess:
        image = self.runner.selection.image
        if image is None:  # a RunnerContext always pins one; keep the type honest
            raise WorkerFailureError("an isolated validation needs a pinned runner image")
        attempt = uuid.uuid4().hex
        operation_id = f"validate:{attempt}"
        out_path = out_path_for(workspace_path) / f"validation-{attempt}"
        out_path.mkdir(parents=True, exist_ok=False)
        # No ``relay_container``: the launcher runs the container with ``--network none``.
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
        receipt_path = spec.receipt_path
        try:
            outcome = read_launch_outcome(
                receipt_path, operation_id=operation_id, what="isolated validation"
            )
            exit_code = require_exited(outcome, what="isolated validation")
        except WorkerFailureError as failure:
            # The container never ran the command to completion: a refused or lost launch is
            # not a failed validation. Classified from the launcher's own outcome, never from
            # its exit status, which the contained command can imitate.
            raise WorkerFailureError(
                str(failure),
                details={**failure.details, "stderr": completed.stderr.strip()[-2000:]},
            ) from failure
        receipt = read_runner_receipt(Path(receipt_path), what="isolated validation receipt")
        mismatches = validation_receipt_mismatches(receipt, self.runner, operation_id)
        if mismatches:
            raise WorkerFailureError(
                "the isolated validation receipt does not describe this run under the profile",
                details={"mismatches": "; ".join(mismatches)[-2000:]},
            )
        reference = publish_receipt(receipt, ArtifactStore(self.artifact_root))
        output = _worker_output(out_path / _RESULT_NAME, nonce=outcome.nonce)
        return AttestedProcess(
            command,
            exit_code,
            redact_credential_shaped_text(output),
            "",
            runner_receipt=reference,
        )


def _worker_output(result_path: Path, *, nonce: str) -> str:
    """Read the in-container wrapper's captured output; the file is untrusted worker data."""
    text = read_bounded_text(result_path, limit=MAX_RESULT_BYTES, what="validation result")
    payload = json.loads(bind_result_to_attempt(text, nonce=nonce, what="validation result"))
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, str):
        raise WorkerFailureError("the isolated validation result has no output")
    return output


__all__ = ["OciValidationRunner"]
