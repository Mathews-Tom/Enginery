"""Launching and collecting an omp worker in the isolated container profile (no engine needed)."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from tests.support.runner_context import (
    harness_task,
    isolated_context,
    runner_receipt,
    write_launch_outcome,
    write_runner_receipt,
    write_worker_result,
)

from enginery.adapters.oci.launch import container_name_for
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.adapters.runner_launch import MAX_RESULT_BYTES, runner_receipt_path
from enginery.application.execution_controls import extract_native_arguments
from enginery.application.work_ports import HarnessResult
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError, WorkerFailureError
from enginery.domain.execution_receipt import ExecutionReceipt
from enginery.domain.runner import (
    DEFAULT_ENV_ALLOWLIST,
    WORKSPACE_MOUNT,
    LaunchSpec,
    RunnerContext,
    RunnerProfile,
    isolated_permission_mapping,
)
from enginery.engine.omp_worker import run_omp_worker
from enginery.ledger.artifact_store import ArtifactStore

_PLANTED_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
_PLANTED_ASSIGNMENT = "API_KEY=sk-live-0123456789abcdefghijklmnop"
_IMAGE_OMP = "omp v18.10.3\n\nUSAGE\n"


def _events(extra: str = "") -> str:
    records: list[dict[str, object]] = [
        {"type": "session", "version": 3},
        {"type": "agent_start"},
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "tool_execution_end", "toolName": "bash", "note": extra},
        {"type": "agent_end"},
    ]
    return "\n".join(json.dumps(record) for record in records)


def _harness(
    tmp_path: Path, *, runner: RunnerContext | None = None, installed: str | None = None
) -> tuple[OmpHarness, list[tuple[str, ...]]]:
    probes: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        probes.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=installed or _IMAGE_OMP, stderr="")

    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        command_runner=run,
        runner=isolated_context(tmp_path / "receipts") if runner is None else runner,
    )
    return harness, probes


def _execution_receipt(result: HarnessResult) -> ExecutionReceipt:
    assert result.execution_receipt is not None
    return result.execution_receipt


def _spec_from(command: tuple[str, ...]) -> LaunchSpec:
    encoded = command[command.index("--spec") + 1]
    assert encoded == json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":"))
    return LaunchSpec.from_state(json.loads(encoded))


def test_supervised_command_wraps_the_in_container_command_in_the_launcher(
    tmp_path: Path,
) -> None:
    harness, _ = _harness(tmp_path)
    task = harness_task(tmp_path / "workspace", "omp")
    result_path = tmp_path / "out" / "result.json"

    command = harness.supervised_command(task, result_path=result_path)

    assert command[:4] == (sys.executable, "-m", "enginery.adapters.oci.launcher", "--spec")
    boundary = command.index("--")
    assert boundary == 5  # exactly one wrapper argument value (the spec) before the first `--`
    assert command[boundary + 1 :] == harness.command_for(task)
    assert command[boundary + 1] == "omp"
    assert WORKSPACE_MOUNT in command[boundary + 1 :]
    assert str(task.workspace_path) not in command[boundary + 1 :]


def test_launch_spec_round_trips_and_binds_the_attempt(tmp_path: Path) -> None:
    harness, _ = _harness(tmp_path)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-spec-1")
    result_path = tmp_path / "out" / "result.json"

    spec = _spec_from(harness.supervised_command(task, result_path=result_path))

    assert spec.operation_id == "op-spec-1"
    assert spec.container_name == container_name_for("op-spec-1")
    assert spec.image == "sha256:" + "a" * 64
    assert spec.profile.digest == RunnerProfile().digest
    assert spec.harness == "omp"
    assert spec.workspace_path == str(task.workspace_path)
    assert spec.out_path == str(result_path.parent)
    assert spec.result_name == "result.json"
    assert spec.receipt_path == f"{tmp_path / 'receipts'}/op-spec-1.runner-receipt.json"
    assert spec.time_budget_seconds == 60
    assert spec.permissions == isolated_permission_mapping("omp")
    assert spec.env == harness.container_environment(task)
    assert spec.bundle_branch is None
    assert spec.bundle_base is None
    assert spec.docker_context is None


def test_launch_spec_carries_the_branch_bundle_and_engine_context(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts", docker_context="lima-enginery")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(
        tmp_path / "workspace", "omp", head_branch="enginery/node-1", base_revision="a" * 40
    )

    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))

    assert spec.bundle_branch == "enginery/node-1"
    assert spec.bundle_base == "a" * 40
    assert spec.docker_context == "lima-enginery"


def test_a_branch_without_its_base_revision_is_not_bundled(tmp_path: Path) -> None:
    harness, _ = _harness(tmp_path)
    task = harness_task(tmp_path / "workspace", "omp", head_branch="enginery/node-1")

    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))

    assert spec.bundle_branch is None
    assert spec.bundle_base is None


def test_container_name_agrees_with_the_launcher_rule(tmp_path: Path) -> None:
    harness, _ = _harness(tmp_path)
    for operation_id in ("op-1", "an operation with spaces", "ünïcode"):
        task = harness_task(tmp_path / "workspace", "omp", operation_id=operation_id)
        spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))
        assert spec.container_name == container_name_for(operation_id)


def test_container_environment_is_an_explicit_allowlisted_set_without_credentials(
    tmp_path: Path,
) -> None:
    harness, _ = _harness(tmp_path)
    environment = dict(harness.container_environment(harness_task(tmp_path, "omp")))

    assert set(environment) <= set(DEFAULT_ENV_ALLOWLIST)
    assert environment["HOME"] == "/home/worker"
    assert environment["TMPDIR"] == "/tmp"
    assert environment["LANG"] == "C.UTF-8"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_NOSYSTEM" not in environment  # the image's system safe.directory must apply
    assert environment["GIT_AUTHOR_NAME"] == environment["GIT_COMMITTER_NAME"] == "Enginery Worker"
    # Git hooks are disabled with command-scoped config that a planted repository config cannot
    # override, so an allowed `git commit` never runs a hook the worker wrote.
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert environment["GIT_AUTHOR_EMAIL"] == environment["GIT_COMMITTER_EMAIL"]
    assert environment["PI_CODING_AGENT_DIR"] == "/home/worker/.omp/agent"
    # Model access is granted by a separate, explicit step; none of it is part of this set.
    credential_shaped = {
        key for key in environment if "TOKEN" in key or "KEY" in key or "URL" in key
    }
    assert credential_shaped == {"GIT_CONFIG_KEY_0"}  # a config key *name*, not a credential
    assert "ANTHROPIC_API_KEY" not in environment
    assert not any("claude" in key.lower() for key in environment)


def test_trusted_local_has_no_container_environment(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )

    with pytest.raises(InvalidInputError, match="only an isolated"):
        harness.container_environment(harness_task(tmp_path, "omp"))


def test_an_isolated_worker_is_never_started_directly_on_the_host(tmp_path: Path) -> None:
    harness, _ = _harness(tmp_path)

    with pytest.raises(WorkerFailureError, match="supervised launcher"):
        harness.start(harness_task(tmp_path, "omp"))


def test_installed_host_is_probed_inside_the_image_through_the_command_runner(
    tmp_path: Path,
) -> None:
    harness, probes = _harness(tmp_path, installed="omp v18.10.3\n\nUSAGE\n")

    installed = harness.installed_host()

    assert probes == [("omp", "--help")]
    assert installed is not None
    assert installed.executable_version == "18.10.3"


def test_native_controls_are_validated_against_the_in_image_version(tmp_path: Path) -> None:
    harness, probes = _harness(tmp_path, installed="omp v18.10.3\n\nUSAGE\n")
    task = harness_task(tmp_path / "workspace", "omp")
    pinned = harness_task(tmp_path / "workspace", "omp", model="openai/gpt-5.2")

    command = harness.command_for(pinned)

    assert command[command.index("--model") + 1] == "openai/gpt-5.2"
    assert ("omp", "--help") in probes
    assert "--model" not in harness.command_for(task)


def test_an_out_of_range_in_image_version_rejects_native_controls(tmp_path: Path) -> None:
    harness, _ = _harness(tmp_path, installed="omp v19.0.0\n\nUSAGE\n")
    pinned = harness_task(tmp_path / "workspace", "omp", model="openai/gpt-5.2")

    with pytest.raises(MissingPrerequisiteError):
        harness.command_for(pinned)


def test_a_launch_probes_the_image_and_records_its_harness_identity_even_without_a_pin(
    tmp_path: Path,
) -> None:
    harness, probes = _harness(tmp_path)
    task = harness_task(tmp_path / "workspace", "omp")  # host default: no native control pinned

    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))

    host = harness.catalog.host("omp")
    assert probes == [("omp", "--help")]
    assert spec.harness_version == host.tested_executable_version == "18.10.3"
    assert spec.harness_protocol == host.protocol_fingerprint


def test_the_image_is_probed_once_for_all_launches_of_one_harness(tmp_path: Path) -> None:
    harness, probes = _harness(tmp_path)

    for operation_id in ("op-one-1", "op-two-1"):
        task = harness_task(tmp_path / "workspace", "omp", operation_id=operation_id)
        harness.supervised_command(task, result_path=tmp_path / "out" / "r.json")

    assert probes == [("omp", "--help")]


@pytest.mark.parametrize(
    "reported", ["omp v18.10.4\n\nUSAGE\n", "omp v19.0.0\n\nUSAGE\n", "not omp at all\n"]
)
def test_a_drifted_or_unidentifiable_image_harness_is_never_launched(
    tmp_path: Path, reported: str
) -> None:
    harness, _ = _harness(tmp_path, installed=reported)
    task = harness_task(tmp_path / "workspace", "omp")  # no pin: only the always-probe catches it

    with pytest.raises(MissingPrerequisiteError, match="omp"):
        harness.supervised_command(task, result_path=tmp_path / "out" / "r.json")


def test_collect_redacts_untrusted_output_and_records_the_runner_receipt(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-collect-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(
        result_path,
        operation_id="op-collect-1",
        output=_events(f"{_PLANTED_TOKEN} {_PLANTED_ASSIGNMENT} trailing"),
    )
    receipt = write_runner_receipt(runner, "op-collect-1", "omp")
    command = harness.supervised_command(task, result_path=result_path)

    events, result = harness.collect_supervised(
        task, result_path=result_path, launched_command=command
    )

    published = harness.artifact_store.read_bytes(result.outputs[0].digest).decode()
    assert _PLANTED_TOKEN not in published
    assert "sk-live-0123456789abcdefghijklmnop" not in published
    assert "[REDACTED:" in published
    assert events[-1].kind.value == "terminal"
    assert result.terminal_status == "succeeded"
    reference = _execution_receipt(result).runner
    assert reference is not None
    assert reference.trust_mode == "isolated-oci"
    assert reference.receipt_digest == receipt.digest
    assert reference.profile_digest == runner.profile.digest
    assert reference.image_id == "sha256:" + "a" * 64
    stored = json.loads(harness.artifact_store.read_bytes(reference.receipt_digest))
    assert stored["operation_id"] == "op-collect-1"
    assert stored["permissions"]["name"] == "omp-isolated-write-allowlist"


def test_collect_reads_submitted_arguments_only_after_the_first_boundary(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner, installed="omp v18.10.3\n\nUSAGE\n")
    task = harness_task(
        tmp_path / "workspace", "omp", operation_id="op-args-1", model="openai/gpt-5.2"
    )
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-args-1", output=_events())
    write_runner_receipt(runner, "op-args-1", "omp")
    command = harness.supervised_command(task, result_path=result_path)

    _, result = harness.collect_supervised(task, result_path=result_path, launched_command=command)

    assert _execution_receipt(result).submitted_arguments == ("--model", "openai/gpt-5.2")
    host = harness.catalog.host("omp")
    assert extract_native_arguments(command, host=host) == ("--model", "openai/gpt-5.2")


def test_a_succeeded_result_beside_a_nonzero_container_exit_is_not_accepted(
    tmp_path: Path,
) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-forged-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-forged-1", output=_events())
    write_runner_receipt(runner, "op-forged-1", "omp")
    # The nonce-bound result says "succeeded", but the container was killed (exit 137).
    write_launch_outcome(runner, "op-forged-1", exit_code=137)

    with pytest.raises(WorkerFailureError, match="claims success but its container exited"):
        harness.collect_supervised(task, result_path=result_path)


def test_a_failed_result_beside_a_nonzero_container_exit_is_still_collected(
    tmp_path: Path,
) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-failed-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(
        result_path, operation_id="op-failed-1", output=_events(), terminal_status="failed"
    )
    write_runner_receipt(runner, "op-failed-1", "omp")
    write_launch_outcome(runner, "op-failed-1", exit_code=1)

    _, result = harness.collect_supervised(task, result_path=result_path)

    assert result.terminal_status == "failed"


def test_collect_without_a_runner_receipt_is_a_worker_failure(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-missing-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-missing-1", output=_events())
    write_launch_outcome(runner, "op-missing-1")

    with pytest.raises(WorkerFailureError, match="runner receipt is missing"):
        harness.collect_supervised(task, result_path=result_path)


def test_collect_rejects_a_receipt_for_another_attempt(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-real-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-real-1", output=_events())
    other = write_runner_receipt(runner, "op-other-1", "omp")
    stolen = Path(runner_receipt_path(runner, "op-real-1"))
    stolen.write_text(json.dumps(other.to_state()), encoding="utf-8")
    write_launch_outcome(runner, "op-real-1")

    with pytest.raises(WorkerFailureError, match="does not match"):
        harness.collect_supervised(task, result_path=result_path)


def test_collect_rejects_a_malformed_receipt(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-bad-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-bad-1", output=_events())
    path = Path(runner_receipt_path(runner, "op-bad-1"))
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version": 1}', encoding="utf-8")
    write_launch_outcome(runner, "op-bad-1")

    with pytest.raises(WorkerFailureError, match="malformed"):
        harness.collect_supervised(task, result_path=result_path)


def test_collect_refuses_an_oversized_worker_result(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-big-1")
    result_path = tmp_path / "out" / "result.json"
    result_path.parent.mkdir(parents=True)
    with result_path.open("wb") as handle:
        handle.truncate(MAX_RESULT_BYTES + 1)
    write_runner_receipt(runner, "op-big-1", "omp")

    with pytest.raises(WorkerFailureError, match="exceeds"):
        harness.collect_supervised(task, result_path=result_path)


def test_collect_rejects_a_result_for_another_operation_even_with_a_receipt(
    tmp_path: Path,
) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-a-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-b-1", output=_events())
    write_runner_receipt(runner, "op-a-1", "omp")

    with pytest.raises(WorkerFailureError, match="does not match task"):
        harness.collect_supervised(task, result_path=result_path)


def test_the_published_receipt_digest_is_the_receipts_own_digest(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-digest-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-digest-1", output=_events())
    receipt = write_runner_receipt(runner, "op-digest-1", "omp")

    _, result = harness.collect_supervised(task, result_path=result_path)

    reference = _execution_receipt(result).runner
    assert reference is not None
    assert Digest.of_bytes(harness.artifact_store.read_bytes(reference.receipt_digest)) == (
        receipt.digest
    )


def test_an_isolated_harness_refuses_an_explicit_unconditional_bypass_mapping(
    tmp_path: Path,
) -> None:
    from enginery.domain.runner import trusted_local_permission_mapping

    with pytest.raises(InvalidInputError, match="unconditional permission bypass"):
        OmpHarness(
            OmpAdapterConfig(credential_reference="ref"),
            ArtifactStore(tmp_path / "artifacts"),
            runner=isolated_context(tmp_path),
            permissions=trusted_local_permission_mapping("omp"),
        )


def _model_events() -> str:
    records: list[dict[str, object]] = [
        {"type": "session", "version": 3},
        {"type": "agent_start"},
        {
            "type": "message_end",
            "message": {"role": "assistant", "provider": "openai", "model": "gpt-5.2"},
        },
        {"type": "agent_end"},
    ]
    return "\n".join(json.dumps(record) for record in records)


def test_the_retained_runner_receipt_records_the_probed_harness_identity(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-identity-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-identity-1", output=_events())
    write_runner_receipt(runner, "op-identity-1", "omp")

    _, result = harness.collect_supervised(task, result_path=result_path)

    reference = _execution_receipt(result).runner
    assert reference is not None
    stored = json.loads(harness.artifact_store.read_bytes(reference.receipt_digest))
    assert stored["harness_version"] == "18.10.3"
    assert stored["harness_protocol"] == harness.catalog.host("omp").protocol_fingerprint


@pytest.mark.parametrize(
    "overrides",
    [
        {"harness_version": None},
        {"harness_protocol": None},
        {"harness_version": "18.10.2"},
        {"harness_protocol": "json-protocol=2"},
    ],
)
def test_collect_rejects_a_receipt_that_does_not_prove_the_cataloged_harness(
    tmp_path: Path, overrides: dict[str, str | None]
) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-proof-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-proof-1", output=_events())
    receipt = replace(
        runner_receipt(runner, "op-proof-1", permissions=isolated_permission_mapping("omp")),
        **overrides,
    )
    path = Path(runner_receipt_path(runner, "op-proof-1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_state()), encoding="utf-8")
    write_launch_outcome(runner, "op-proof-1")

    with pytest.raises(WorkerFailureError, match="does not match") as raised:
        harness.collect_supervised(task, result_path=result_path)

    assert next(iter(overrides)) in str(raised.value.details["mismatches"])


def test_an_isolated_workers_model_id_is_labelled_as_worker_reported(tmp_path: Path) -> None:
    runner = isolated_context(tmp_path / "receipts")
    harness, _ = _harness(tmp_path, runner=runner)
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-model-1")
    result_path = tmp_path / "out" / "result.json"
    write_worker_result(result_path, operation_id="op-model-1", output=_model_events())
    write_runner_receipt(runner, "op-model-1", "omp")

    _, result = harness.collect_supervised(task, result_path=result_path)

    receipt = _execution_receipt(result)
    assert receipt.effective_model.value == "openai/gpt-5.2"
    assert receipt.effective_model.reported_by == "worker"
    assert receipt.to_state()["effective_model"]["reported_by"] == "worker"  # type: ignore[index]
    assert receipt.effective_effort.reported_by is None  # an unknown control asserts nothing


def test_a_trusted_local_models_receipt_is_unlabelled_and_unchanged(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    task = harness_task(tmp_path / "workspace", "omp", operation_id="op-local-1")
    result_path = tmp_path / "result.json"
    run_omp_worker(
        operation_id="op-local-1",
        command=(sys.executable, "-c", f"print({json.dumps(_model_events())})"),
        output_path=result_path,
    )

    _, result = harness.collect_supervised(task, result_path=result_path)

    receipt = _execution_receipt(result)
    assert receipt.effective_model.value == "openai/gpt-5.2"
    assert "reported_by" not in receipt.to_state()["effective_model"]  # type: ignore[operator]
