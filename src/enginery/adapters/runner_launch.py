"""Launch and result-intake helpers shared by the harness adapters' isolated profile.

An isolated worker runs inside a hardened container that a separate host-side launcher process
creates and supervises. The adapters therefore never start the harness directly in that mode:
they describe the attempt as a :class:`~enginery.domain.runner.LaunchSpec` and wrap the
in-container harness command in the launcher's command line. Everything the container hands
back (the result file, the runner receipt) is untrusted input and is bounded and validated here
before an adapter uses it.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
import sys
from pathlib import Path

from enginery.adapters.oci.launch import container_name_for
from enginery.application.work_ports import HarnessTask
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError, WorkerFailureError
from enginery.domain.execution_catalog import HostCapability, InstalledHost
from enginery.domain.execution_receipt import RunnerReceiptRef
from enginery.domain.runner import (
    RELAY_HOSTNAME,
    WORKER_HOME,
    WORKER_TMP,
    LaunchOutcome,
    LaunchSpec,
    OutcomeState,
    PermissionMapping,
    RunnerContext,
    RunnerReceipt,
    TrustMode,
    expected_network,
    outcome_path_for,
    profile_violations,
)
from enginery.ledger.artifact_store import ArtifactStore

LAUNCHER_MODULE = "enginery.adapters.oci.launcher"
MAX_RESULT_BYTES = 64 * 1024 * 1024
"""The largest worker result file the host reads; the worker is untrusted."""

MAX_RECEIPT_BYTES = 1024 * 1024
"""The largest runner receipt file the host reads."""

MAX_OUTCOME_BYTES = 64 * 1024
"""The largest launcher outcome file the host reads."""

_READ_CHUNK = 1024 * 1024

_WORKER_IDENTITY = ("Enginery Worker", "worker@enginery.invalid")
_WORKER_PATH = "/usr/local/bin:/usr/bin:/bin"


def worker_environment(
    harness_environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """The explicit, non-secret environment of an isolated worker.

    Nothing is inherited from the host. Model-endpoint and credential variables are never part
    of it; model access is granted separately and explicitly. Git hooks are disabled through
    command-scoped configuration, which repository configuration cannot override, so a hook the
    worker plants is not run by an allowed ``git commit``.
    """
    name, email = _WORKER_IDENTITY
    return (
        ("GIT_AUTHOR_EMAIL", email),
        ("GIT_AUTHOR_NAME", name),
        ("GIT_COMMITTER_EMAIL", email),
        ("GIT_COMMITTER_NAME", name),
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "core.hooksPath"),
        ("GIT_CONFIG_VALUE_0", "/dev/null"),
        ("GIT_TERMINAL_PROMPT", "0"),
        ("HOME", WORKER_HOME),
        ("LANG", "C.UTF-8"),
        ("PATH", _WORKER_PATH),
        ("TMPDIR", WORKER_TMP),
        *harness_environment,
    )


OMP_MODELS_PATH = f"{WORKER_HOME}/.omp/agent/models.yml"
"""Where omp reads its provider overrides; the agent directory is task-local tmpfs."""

_OMP_RELAY_PROVIDERS = ("anthropic", "openai-codex")


def relay_claude_environment(runner: RunnerContext) -> tuple[tuple[str, str], ...]:
    """Claude Code's model endpoint and placeholder credential, or nothing without a relay.

    The harness-level switches that keep it from phoning home (telemetry, updater, nonessential
    traffic) are part of the harness environment itself. ``ANTHROPIC_API_KEY`` is never set.
    """
    relay = runner.relay
    if relay is None:
        return ()
    return (
        ("ANTHROPIC_AUTH_TOKEN", relay.dummy_credential),
        ("ANTHROPIC_BASE_URL", relay.url),
    )


def relay_omp_seed_files(runner: RunnerContext) -> tuple[tuple[str, str], ...]:
    """The task-local ``models.yml`` that points omp's providers at the relay, or nothing.

    Each provider uses the pi-native transport, so omp posts ``/v1/pi/stream`` to the relay with
    ``modelId: <provider>/<model>``. The ``apiKey`` is the placeholder credential.
    """
    relay = runner.relay
    if relay is None:
        return ()
    lines = ["providers:"]
    for provider in _OMP_RELAY_PROVIDERS:
        lines += [
            f"  {provider}:",
            f"    baseUrl: {relay.url}",
            f"    apiKey: {relay.dummy_credential}",
            "    transport: pi-native",
        ]
    return ((OMP_MODELS_PATH, "\n".join(lines) + "\n"),)


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
    image_host: InstalledHost | None = None,
    seed_files: tuple[tuple[str, str], ...] = (),
) -> tuple[str, ...]:
    """The supervised host command: the launcher, its spec, ``--``, then the in-container argv.

    ``image_host`` is the in-image harness version and protocol the adapter verified against the
    execution catalog; the launcher records it in the runner receipt.
    """
    if not result_path.is_absolute():
        raise InvalidInputError("worker result path must be absolute")
    image = runner.selection.image
    if image is None:  # a RunnerContext always pins one; keep the type honest
        raise InvalidInputError("an isolated runner selection must pin an image")
    operation_id = str(task.operation_id)
    bundle = task.head_branch is not None and task.base_revision is not None
    spec = LaunchSpec(
        operation_id=operation_id,
        container_name=container_name_for(operation_id),
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
        seed_files=seed_files,
        bundle_branch=task.head_branch if bundle else None,
        bundle_base=task.base_revision if bundle else None,
        docker_context=runner.selection.docker_context,
        harness_version=None if image_host is None else image_host.executable_version,
        harness_protocol=None if image_host is None else image_host.protocol_fingerprint,
        relay_container=RELAY_HOSTNAME if runner.relay is not None else None,
    )
    encoded = json.dumps(spec.to_state(), sort_keys=True, separators=(",", ":"))
    return (sys.executable, "-m", LAUNCHER_MODULE, "--spec", encoded, "--", *inner_command)


def _read_regular_file(path: Path, limit: int, what: str) -> bytes:
    """Read at most ``limit`` bytes of the regular file ``path`` without ever following a link.

    The final component is opened relative to its directory with ``O_NOFOLLOW`` (a symlink is
    refused) and ``O_NONBLOCK`` (a FIFO cannot hang the host); the open descriptor is then
    checked to be a regular file within the cap, so what is read is exactly what was checked.
    """
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise WorkerFailureError(f"{what} is not a regular file")
            if info.st_size > limit:
                raise WorkerFailureError(f"{what} exceeds the {limit}-byte limit")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, _READ_CHUNK))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise WorkerFailureError(f"{what} is missing or unreadable") from error
    data = b"".join(chunks)
    if len(data) > limit:
        raise WorkerFailureError(f"{what} exceeds the {limit}-byte limit")
    return data


def read_bounded_text(path: Path, *, limit: int, what: str) -> str:
    """Read an untrusted file as UTF-8 text.

    Refuses a symlink, a FIFO or any other non-regular file, and anything over ``limit`` bytes;
    nothing here can follow a link out of the worker's directory or block on a pipe.
    """
    data = _read_regular_file(path, limit, what)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkerFailureError(f"{what} is missing or unreadable") from error


def read_launch_outcome(receipt_path: str, *, operation_id: str, what: str) -> LaunchOutcome:
    """The launcher's own record of how one attempt ended (a host-only file beside the receipt)."""
    text = read_bounded_text(
        Path(outcome_path_for(receipt_path)),
        limit=MAX_OUTCOME_BYTES,
        what=f"{what} launcher outcome",
    )
    try:
        outcome = LaunchOutcome.from_state(json.loads(text))
    except (json.JSONDecodeError, InvalidInputError) as error:
        raise WorkerFailureError(f"the {what} launcher outcome is malformed") from error
    if outcome.operation_id != operation_id:
        raise WorkerFailureError(f"the {what} launcher outcome belongs to another operation")
    return outcome


_OUTCOME_FAILURES = {
    OutcomeState.REFUSED: "the isolated runner refused to launch the {what}",
    OutcomeState.DEADLINE: "the {what} passed the launcher's hard deadline and was killed",
    OutcomeState.INTERRUPTED: "the launcher of the {what} was stopped before the container ended",
    OutcomeState.FAULTED: "the launcher of the {what} faulted",
    OutcomeState.LAUNCHING: "the launcher of the {what} did not finish (it never started it)",
    OutcomeState.RUNNING: "the launcher of the {what} did not finish (it was lost while running)",
}


def require_exited(outcome: LaunchOutcome, *, what: str) -> int:
    """The container's exit code, or the refusal that explains why there is none.

    Classified from the launcher's own outcome and never from its process exit status, which a
    contained command can imitate. A refused, timed-out, interrupted, faulted or unfinished
    launch is a worker failure. A write past a disk cap is not an outcome of its own: the kernel
    refuses it inside the container, and what the worker then exits with is an exit like any other.
    """
    if outcome.state is OutcomeState.EXITED and outcome.exit_code is not None:
        return outcome.exit_code
    details = {"state": outcome.state.value, "detail": outcome.detail[-2000:]}
    if outcome.violations:
        details["violations"] = "; ".join(outcome.violations)[-2000:]
    raise WorkerFailureError(_OUTCOME_FAILURES[outcome.state].format(what=what), details=details)


def bind_result_to_attempt(text: str, *, nonce: str, what: str) -> str:
    """The worker's result JSON without its ``nonce`` key, only if it echoes this launch's nonce.

    A result left by an earlier attempt, or written without the nonce, cannot be replayed into
    this one.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise WorkerFailureError(f"the {what} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise WorkerFailureError(f"the {what} has an invalid shape")
    echoed = payload.pop("nonce", None)
    if not isinstance(echoed, str) or not hmac.compare_digest(
        echoed.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise WorkerFailureError(f"the {what} is not bound to this attempt")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def read_attempt_result(
    runner: RunnerContext, task: HarnessTask, result_path: Path, *, what: str
) -> str:
    """Read one isolated attempt's result: the launcher must have seen the container exit, and
    the result must carry that launch's nonce.

    ``what`` names the worker in messages (``"Claude Code supervised worker"``). Returns the
    result JSON with the nonce removed, ready for the adapter's own validation.

    The nonce proves who wrote the result file, not how the container ended: a worker can read
    the nonce, write a ``"succeeded"`` result and then die. The supervisor in the image reports
    ``"succeeded"`` only for a command that exited 0 and exits with that same code, so a
    ``"succeeded"`` result beside a non-zero container exit cannot come from an honest run. It is
    refused as a worker failure (not held for the operator): the attempt produced no usable
    result and the failure path already retries or escalates it.
    """
    operation_id = str(task.operation_id)
    outcome = read_launch_outcome(
        runner_receipt_path(runner, operation_id), operation_id=operation_id, what=what
    )
    exit_code = require_exited(outcome, what=what)
    text = read_bounded_text(result_path, limit=MAX_RESULT_BYTES, what=f"{what} result")
    bound = bind_result_to_attempt(text, nonce=outcome.nonce, what=f"{what} result")
    if exit_code != 0 and json.loads(bound).get("terminal_status") == "succeeded":
        raise WorkerFailureError(
            f"the {what} result claims success but its container exited with code {exit_code}",
            details={"exit_code": str(exit_code)},
        )
    return bound


def verified_image_host(host: HostCapability, installed: InstalledHost | None) -> InstalledHost:
    """Require the probed in-image harness to be the catalog's tested executable.

    The image pins exactly the version the catalog tested, so a version that is merely
    inside the accepted range is not qualified here. Raises when the harness cannot be
    probed or is any other version or protocol; otherwise returns the verified identity,
    which the launcher records in the runner receipt.
    """
    host.require_installed(installed)
    if installed is None or installed.executable_version != host.tested_executable_version:
        raise MissingPrerequisiteError(
            f"the {host.harness} in the pinned runner image is "
            f"{'unobservable' if installed is None else installed.executable_version}, not "
            f"the tested {host.tested_executable_version}, so the isolated run is not qualified",
            details={
                "harness": host.harness,
                "image_executable_version": (
                    "unobservable" if installed is None else installed.executable_version
                ),
                **host.version.details(),
            },
        )
    return InstalledHost(
        executable_version=host.tested_executable_version,
        protocol_fingerprint=host.protocol_fingerprint,
    )


def read_runner_receipt(path: Path, *, what: str = "runner receipt") -> RunnerReceipt:
    """Read and decode the launcher's receipt file (a host-only file the worker cannot reach).

    A missing, oversized, non-regular or malformed receipt is a worker failure: an attempt
    without proof of what it ran under is never accepted, and no receipt is ever invented.
    """
    text = read_bounded_text(path, limit=MAX_RECEIPT_BYTES, what=what)
    try:
        return RunnerReceipt.from_state(json.loads(text))
    except (json.JSONDecodeError, InvalidInputError) as error:
        raise WorkerFailureError(f"{what} is malformed") from error


def publish_receipt(receipt: RunnerReceipt, artifact_store: ArtifactStore) -> RunnerReceiptRef:
    """Publish one verified receipt (its canonical form) and return the reference to it."""
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


def publish_runner_receipt(
    runner: RunnerContext,
    task: HarnessTask,
    artifact_store: ArtifactStore,
    *,
    expected_host: InstalledHost,
) -> RunnerReceiptRef:
    """Validate the launcher's runner receipt, publish it, and return the reference to it.

    A missing, malformed or mismatching receipt is a worker failure: an isolated attempt
    without proof of what it ran under is never accepted, and no receipt is ever invented.
    The receipt must also record the in-image harness identity the adapter verified, and the
    container it describes must have been attached to exactly the network its run was granted
    (its own attempt network with model access, none without) and satisfy the profile.
    """
    operation_id = str(task.operation_id)
    receipt = read_runner_receipt(Path(runner_receipt_path(runner, operation_id)))
    mismatches = _receipt_mismatches(receipt, runner, operation_id, expected_host)
    if mismatches:
        raise WorkerFailureError(
            "runner receipt does not match the admitted run",
            details={"mismatches": ",".join(mismatches)},
        )
    violations = profile_violations(
        runner.profile, receipt.effective, network=granted_network(runner, operation_id)
    )
    if violations:
        raise WorkerFailureError(
            "runner receipt records a container weaker than the admitted profile",
            details={"violations": "; ".join(violations)[-2000:]},
        )
    return publish_receipt(receipt, artifact_store)


def granted_network(runner: RunnerContext, operation_id: str) -> str:
    """The one network the run's selection permits ``operation_id``'s container to join."""
    return expected_network(runner.profile, operation_id, relay=runner.relay is not None)


def _receipt_mismatches(
    receipt: RunnerReceipt, runner: RunnerContext, operation_id: str, expected_host: InstalledHost
) -> tuple[str, ...]:
    expected_profile: Digest = runner.profile.digest
    checks: tuple[tuple[str, bool], ...] = (
        ("operation", receipt.operation_id == operation_id),
        ("trust_mode", receipt.trust_mode is TrustMode.ISOLATED_OCI),
        ("profile", receipt.profile_digest == expected_profile),
        ("image", receipt.image.image_id == runner.selection.image),
        ("container", receipt.container_name == container_name_for(operation_id)),
        ("harness_version", receipt.harness_version == expected_host.executable_version),
        ("harness_protocol", receipt.harness_protocol == expected_host.protocol_fingerprint),
    )
    return tuple(name for name, ok in checks if not ok)
