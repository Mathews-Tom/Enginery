"""Builders for isolated-runner contexts, receipts and harness tasks used in adapter tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from enginery.adapters.oci.launcher import write_outcome
from enginery.adapters.runner_launch import runner_container_name, runner_receipt_path
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import HarnessTask
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.runner import (
    OUT_MOUNT,
    WORKER_HOME,
    WORKER_TMP,
    WORKSPACE_MOUNT,
    EffectiveContainer,
    EngineIdentity,
    ImageIdentity,
    LaunchOutcome,
    OutcomeState,
    PermissionMapping,
    RunnerContext,
    RunnerProfile,
    RunnerReceipt,
    RunnerSelection,
    TrustMode,
    isolated_permission_mapping,
    outcome_path_for,
)
from tests.support.execution import selection_for

IMAGE_ID = "sha256:" + "a" * 64
LAUNCH_NONCE = "launch-nonce-0123456789"


def isolated_context(receipt_root: Path, *, docker_context: str | None = None) -> RunnerContext:
    """An isolated runner context pinned to a placeholder image id."""
    profile = RunnerProfile()
    return RunnerContext(
        selection=RunnerSelection.isolated(
            image=IMAGE_ID, profile=profile, docker_context=docker_context
        ),
        profile=profile,
        receipt_root=str(receipt_root),
    )


def harness_task(
    workspace: Path,
    harness: str,
    *,
    operation_id: str = "operation-1",
    head_branch: str | None = None,
    base_revision: str | None = None,
    model: str | None = None,
) -> HarnessTask:
    return HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId(operation_id),
        workspace_path=workspace,
        objective="Implement the requested provider behavior.",
        acceptance_criteria=("tests pass",),
        constraints=("no credentials in output",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=Decimal("1.00"),
        selection=selection_for(harness, model=model),
        head_branch=head_branch,
        base_revision=base_revision,
    )


def clean_effective_container(profile: RunnerProfile) -> EffectiveContainer:
    """What an engine reports for a container that satisfies the profile exactly."""
    limits = profile.resources
    return EffectiveContainer(
        user=f"{profile.uid}:{profile.gid}",
        cap_drop=profile.cap_drop,
        cap_add=(),
        security_options=profile.security_options,
        privileged=False,
        read_only_rootfs=True,
        memory_bytes=limits.memory_bytes,
        memory_swap_bytes=limits.memory_bytes,
        nano_cpus=limits.nano_cpus,
        pids_limit=limits.pids,
        network_mode=profile.network,
        networks=(profile.network,),
        mounts=(
            ("bind", WORKSPACE_MOUNT, True),
            ("bind", OUT_MOUNT, True),
            ("tmpfs", WORKER_TMP, True),
            ("tmpfs", WORKER_HOME, True),
        ),
        devices=(),
        env_keys=tuple(profile.env_allowlist),
    )


def runner_receipt(
    context: RunnerContext, operation_id: str, *, permissions: PermissionMapping
) -> RunnerReceipt:
    assert context.selection.image is not None
    host = load_execution_catalog().host(permissions.harness)
    return RunnerReceipt(
        operation_id=operation_id,
        trust_mode=TrustMode.ISOLATED_OCI,
        profile_name=context.profile.name,
        profile_digest=context.profile.digest,
        engine=EngineIdentity(
            kind="docker",
            server_version="29.0.0",
            operating_system="Linux",
            kernel_version="6.8.0",
            cgroup_version="2",
            security_options=("name=seccomp", "name=cgroupns"),
            rootless=True,
        ),
        image=ImageIdentity(image_id=context.selection.image, recipe_digest="sha256:" + "b" * 64),
        container_name=runner_container_name(operation_id),
        host={"os": "linux"},
        effective=clean_effective_container(context.profile),
        permissions=permissions,
        harness_version=host.tested_executable_version,
        harness_protocol=host.protocol_fingerprint,
        started_at="2026-10-13T00:00:00Z",
    )


def write_launch_outcome(
    context: RunnerContext,
    operation_id: str,
    *,
    state: OutcomeState = OutcomeState.EXITED,
    nonce: str = LAUNCH_NONCE,
) -> LaunchOutcome:
    """Write, where the launcher would, the host-only outcome of one launch."""
    outcome = LaunchOutcome(
        operation_id=operation_id,
        nonce=nonce,
        state=state,
        exit_code=0 if state is OutcomeState.EXITED else None,
    )
    write_outcome(outcome_path_for(runner_receipt_path(context, operation_id)), outcome)
    return outcome


def write_runner_receipt(
    context: RunnerContext, operation_id: str, harness: str, *, exited: bool = True
) -> RunnerReceipt:
    """Write, where the launcher would, a valid runner receipt (and its exited outcome)."""
    receipt = runner_receipt(
        context, operation_id, permissions=isolated_permission_mapping(harness)
    )
    path = Path(runner_receipt_path(context, operation_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_state(), sort_keys=True), encoding="utf-8")
    if exited:
        write_launch_outcome(context, operation_id)
    return receipt


def write_worker_result(
    path: Path,
    *,
    operation_id: str,
    output: str,
    terminal_status: str = "succeeded",
    nonce: str = LAUNCH_NONCE,
) -> None:
    """Write a worker result file in the schema the in-container supervisor produces."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "nonce": nonce,
                "operation_id": operation_id,
                "output": output,
                "schema_version": 1,
                "terminal_status": terminal_status,
            }
        ),
        encoding="utf-8",
    )
