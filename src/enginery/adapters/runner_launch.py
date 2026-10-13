"""Launch and result-intake helpers shared by the harness adapters' isolated profile.

An isolated worker runs inside a hardened container that a separate host-side launcher process
creates and supervises. The adapters therefore never start the harness directly in that mode:
they describe the attempt as a :class:`~enginery.domain.runner.LaunchSpec` and wrap the
in-container harness command in the launcher's command line. Everything the container hands
back (the result file, the runner receipt) is untrusted input and is bounded and validated here
before an adapter uses it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from enginery.application.work_ports import HarnessTask
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, WorkerFailureError
from enginery.domain.execution_receipt import RunnerReceiptRef
from enginery.domain.runner import (
    WORKER_HOME,
    WORKER_TMP,
    LaunchSpec,
    PermissionMapping,
    RunnerContext,
    RunnerReceipt,
    TrustMode,
)
from enginery.ledger.artifact_store import ArtifactStore

LAUNCHER_MODULE = "enginery.adapters.oci.launcher"
MAX_RESULT_BYTES = 64 * 1024 * 1024
"""The largest worker result file the host reads; the worker is untrusted."""

MAX_RECEIPT_BYTES = 1024 * 1024
"""The largest runner receipt file the host reads."""

_WORKER_IDENTITY = ("Enginery Worker", "worker@enginery.invalid")
_WORKER_PATH = "/usr/local/bin:/usr/bin:/bin"


def runner_container_name(operation_id: str) -> str:
    """The deterministic container name for one operation (stable across resumes)."""
    return "enginery-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]


def worker_environment(
    harness_environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """The explicit, non-secret environment of an isolated worker.

    Nothing is inherited from the host. Model-endpoint and credential variables are never part
    of it; model access is granted separately and explicitly.
    """
    name, email = _WORKER_IDENTITY
    return (
        ("GIT_AUTHOR_EMAIL", email),
        ("GIT_AUTHOR_NAME", name),
        ("GIT_COMMITTER_EMAIL", email),
        ("GIT_COMMITTER_NAME", name),
        ("GIT_TERMINAL_PROMPT", "0"),
        ("HOME", WORKER_HOME),
        ("LANG", "C.UTF-8"),
        ("PATH", _WORKER_PATH),
        ("TMPDIR", WORKER_TMP),
        *harness_environment,
    )


def require_isolated_permissions(permissions: PermissionMapping) -> None:
    """Refuse an isolated launch that would run with permission checks off."""
    if permissions.unconditional_bypass:
        raise InvalidInputError(
            "an isolated worker cannot run with an unconditional permission bypass",
            details={"permissions": permissions.name},
        )


def runner_receipt_path(runner: RunnerContext, operation_id: str) -> str:
    """Where the launcher writes the runner receipt of one operation (outside worker mounts)."""
    return f"{runner.receipt_root}/{operation_id}.runner-receipt.json"


def isolated_command(
    *,
    runner: RunnerContext,
    harness: str,
    task: HarnessTask,
    result_path: Path,
    permissions: PermissionMapping,
    environment: tuple[tuple[str, str], ...],
    inner_command: tuple[str, ...],
) -> tuple[str, ...]:
    """The supervised host command: the launcher, its spec, ``--``, then the in-container argv."""
    if not result_path.is_absolute():
        raise InvalidInputError("worker result path must be absolute")
    image = runner.selection.image
    if image is None:  # a RunnerContext always pins one; keep the type honest
        raise InvalidInputError("an isolated runner selection must pin an image")
    operation_id = str(task.operation_id)
    bundle = task.head_branch is not None and task.base_revision is not None
    spec = LaunchSpec(
        operation_id=operation_id,
        container_name=runner_container_name(operation_id),
        image=image,
        profile=runner.profile,
        harness=harness,
        workspace_path=str(task.workspace_path),
        out_path=str(result_path.parent),
        receipt_path=runner_receipt_path(runner, operation_id),
        result_name=result_path.name,
        time_budget_seconds=task.time_budget_seconds,
        permissions=permissions,
        env=environment,
        bundle_branch=task.head_branch if bundle else None,
        bundle_base=task.base_revision if bundle else None,
        docker_context=runner.selection.docker_context,
    )
    encoded = json.dumps(spec.to_state(), sort_keys=True, separators=(",", ":"))
    return (sys.executable, "-m", LAUNCHER_MODULE, "--spec", encoded, "--", *inner_command)


def read_bounded_text(path: Path, *, limit: int, what: str) -> str:
    """Read an untrusted file as UTF-8 text, refusing anything over ``limit`` bytes."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise WorkerFailureError(f"{what} exceeds the {limit}-byte limit")
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkerFailureError(f"{what} is missing or unreadable") from error


def publish_runner_receipt(
    runner: RunnerContext, task: HarnessTask, artifact_store: ArtifactStore
) -> RunnerReceiptRef:
    """Validate the launcher's runner receipt, publish it, and return the reference to it.

    A missing, malformed or mismatching receipt is a worker failure: an isolated attempt
    without proof of what it ran under is never accepted, and no receipt is ever invented.
    """
    operation_id = str(task.operation_id)
    path = Path(runner_receipt_path(runner, operation_id))
    text = read_bounded_text(path, limit=MAX_RECEIPT_BYTES, what="runner receipt")
    try:
        receipt = RunnerReceipt.from_state(json.loads(text))
    except (json.JSONDecodeError, InvalidInputError) as error:
        raise WorkerFailureError("runner receipt is malformed") from error
    mismatches = _receipt_mismatches(receipt, runner, operation_id)
    if mismatches:
        raise WorkerFailureError(
            "runner receipt does not match the admitted run",
            details={"mismatches": ",".join(mismatches)},
        )
    canonical = json.dumps(receipt.to_state(), sort_keys=True, separators=(",", ":"))
    digest = artifact_store.publish_bytes(canonical.encode("utf-8"), media_type="application/json")
    if digest != receipt.digest:  # the published bytes are exactly the receipt's canonical form
        raise WorkerFailureError("runner receipt digest is inconsistent")
    return RunnerReceiptRef(
        trust_mode=receipt.trust_mode.value,
        receipt_digest=digest,
        image_id=receipt.image.image_id,
        profile_digest=receipt.profile_digest,
    )


def _receipt_mismatches(
    receipt: RunnerReceipt, runner: RunnerContext, operation_id: str
) -> tuple[str, ...]:
    expected_profile: Digest = runner.profile.digest
    checks: tuple[tuple[str, bool], ...] = (
        ("operation", receipt.operation_id == operation_id),
        ("trust_mode", receipt.trust_mode is TrustMode.ISOLATED_OCI),
        ("profile", receipt.profile_digest == expected_profile),
        ("image", receipt.image.image_id == runner.selection.image),
        ("container", receipt.container_name == runner_container_name(operation_id)),
    )
    return tuple(name for name, ok in checks if not ok)
