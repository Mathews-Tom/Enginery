"""The isolated omp permission mapping: declared flags and a real-binary refusal proof."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from tests.support.loopback_model import STUB_TOKEN, LoopbackModelServer, ToolRequest
from tests.support.real_harness import (
    ProofWorkspace,
    make_proof_workspace,
    require_real_harness,
    scrubbed_environment,
)
from tests.support.runner_context import harness_task, isolated_context

import enginery.adapters.oci as oci_package
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    WORKSPACE_MOUNT,
    isolated_permission_mapping,
    trusted_local_permission_mapping,
)
from enginery.ledger.artifact_store import ArtifactStore

POLICY_ASSET = Path(oci_package.__file__).parent / "assets" / "omp-isolated-policy.yml"


def _isolated_harness(tmp_path: Path) -> OmpHarness:
    return OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        runner=isolated_context(tmp_path / "receipts"),
    )


def _argument_after(arguments: tuple[str, ...], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


def test_isolated_mapping_is_explicit_and_never_the_implicit_yolo() -> None:
    mapping = isolated_permission_mapping("omp")
    arguments = mapping.arguments

    assert mapping.unconditional_bypass is False
    assert _argument_after(arguments, "--approval-mode") == "write"
    assert "--auto-approve" not in arguments
    assert "--yolo" not in arguments
    assert _argument_after(arguments, "--tools") == "read,edit,write,bash,glob,grep"
    # The overlay lives in the image, outside every worker-writable mount.
    overlay = _argument_after(arguments, "--config")
    assert overlay == "/opt/enginery/omp-isolated-policy.yml"
    assert not overlay.startswith((WORKSPACE_MOUNT, "/out", "/home/worker", "/tmp"))


def test_trusted_local_mapping_is_the_declared_implicit_yolo() -> None:
    mapping = trusted_local_permission_mapping("omp")

    assert mapping.unconditional_bypass is True
    assert mapping.arguments == ()


def test_trusted_local_command_keeps_the_existing_arguments(tmp_path: Path) -> None:
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    task = harness_task(tmp_path, "omp")

    command = harness.command_for(task)

    assert command[:-1] == (
        "omp",
        "--mode=json",
        "--no-session",
        "--cwd",
        str(tmp_path),
        "--max-time",
        "60",
        "-p",
    )
    assert command[-1].startswith("Objective:")


def test_isolated_command_works_in_the_container_with_the_mapping_verbatim(
    tmp_path: Path,
) -> None:
    harness = _isolated_harness(tmp_path)

    command = harness.command_for(harness_task(tmp_path, "omp"))

    assert _argument_after(command, "--cwd") == WORKSPACE_MOUNT
    mapping = isolated_permission_mapping("omp").arguments
    start = command.index(mapping[0])
    assert command[start : start + len(mapping)] == mapping
    assert str(tmp_path) not in command  # no host path reaches the container
    assert command.index("--approval-mode") < command.index("-p")


def test_isolated_harness_refuses_a_bypass_override(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="unconditional permission bypass"):
        OmpHarness(
            OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
            ArtifactStore(tmp_path / "artifacts"),
            permissions=trusted_local_permission_mapping("omp"),
            runner=isolated_context(tmp_path / "receipts"),
        )


def test_policy_overlay_denies_publishing_and_allows_only_listed_commands() -> None:
    policy = yaml.safe_load(POLICY_ASSET.read_text(encoding="utf-8"))
    rules = policy["bash"]["patterns"]
    approvals = {rule["match"]: rule["approval"] for rule in rules}

    assert policy["tools"]["approval"]["bash"] == "prompt"
    assert approvals["git push*"] == "deny"
    assert approvals["git commit*"] == "allow"
    assert "*" not in approvals  # no catch-all rule can ever allow an unlisted command
    # Every deny rule precedes every allow rule: the first matching rule wins.
    order = [rule["approval"] for rule in rules]
    assert order == sorted(order, key=lambda value: value != "deny")


@dataclass(frozen=True, slots=True)
class _Outcome:
    is_error: bool
    text: str


def _tool_outcomes(json_events: str) -> dict[str, _Outcome]:
    outcomes: dict[str, _Outcome] = {}
    for line in json_events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "tool_execution_end":
            outcomes[event["toolCallId"]] = _Outcome(
                bool(event.get("isError")), json.dumps(event.get("result"))
            )
    return outcomes


def _host_command(harness: OmpHarness, proof: ProofWorkspace, binary: Path) -> tuple[str, ...]:
    """The adapter's isolated argv with its two container-only paths pointed at the host."""
    command = harness.command_for(harness_task(proof.workspace, "omp"))
    translated = [
        str(proof.workspace)
        if argument == WORKSPACE_MOUNT
        else str(POLICY_ASSET)
        if argument == "/opt/enginery/omp-isolated-policy.yml"
        else argument
        for argument in command
    ]
    prompt_flag = translated.index("-p")
    translated[prompt_flag:prompt_flag] = ["--model", "anthropic/claude-sonnet-5-5"]
    return (str(binary), *translated[1:])


def _run_omp(
    harness: OmpHarness, proof: ProofWorkspace, binary: Path, server: LoopbackModelServer
) -> str:
    task = harness_task(proof.workspace, "omp")
    environment = scrubbed_environment(binary, proof, dict(harness.container_environment(task)))
    (proof.home / ".omp" / "agent" / "models.yml").write_text(
        textwrap.dedent(
            f"""\
            providers:
              anthropic:
                baseUrl: {server.base_url}
                apiKey: {STUB_TOKEN}
                transport: pi-native
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        _host_command(harness, proof, binary),
        cwd=proof.workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    return completed.stdout


def test_real_omp_refuses_disallowed_tool_calls_and_runs_allowed_ones(tmp_path: Path) -> None:
    binary = require_real_harness("omp")
    proof = make_proof_workspace(tmp_path)
    allowed_file = proof.workspace / "allowed.txt"
    turns = [
        [
            ToolRequest("write", {"path": str(allowed_file), "content": "allowed\n"}),
            ToolRequest("bash", {"command": "git push origin HEAD"}),
            ToolRequest("bash", {"command": f"touch {proof.workspace / 'shell-marker'}"}),
            ToolRequest("bash", {"command": "git status && git push origin HEAD"}),
            ToolRequest("bash", {"command": "curl http://127.0.0.1:9/"}),
            ToolRequest("eval", {"language": "py", "code": "print(1)"}),
        ],
        [ToolRequest("bash", {"command": "git add allowed.txt"})],
        [ToolRequest("bash", {"command": "git commit -m 'add allowed file'"})],
    ]
    harness = _isolated_harness(tmp_path)

    with LoopbackModelServer("pi-native", turns) as server:
        output = _run_omp(harness, proof, binary, server)

    outcomes = _tool_outcomes(output)
    # Allowed: a write in the workspace and the two local git steps.
    assert not outcomes["call_0_0"].is_error
    assert allowed_file.read_text(encoding="utf-8") == "allowed\n"
    assert not outcomes["call_1_0"].is_error
    assert not outcomes["call_2_0"].is_error
    assert proof.git("log", "-1", "--format=%s") == "add allowed file\n"
    assert proof.git("show", "--name-only", "--format=", "HEAD") == "allowed.txt\n"
    # Refused by the pattern deny rules, by the missing approval for an unlisted command, and
    # because the tool is not offered. None of them left a side effect.
    assert outcomes["call_0_1"].is_error
    assert "blocked by tool policy" in outcomes["call_0_1"].text
    assert proof.remote_refs() == ""
    assert outcomes["call_0_2"].is_error
    assert "requires approval" in outcomes["call_0_2"].text
    assert not (proof.workspace / "shell-marker").exists()
    assert outcomes["call_0_3"].is_error
    assert "blocked by tool policy" in outcomes["call_0_3"].text
    assert outcomes["call_0_4"].is_error
    assert "blocked by tool policy" in outcomes["call_0_4"].text
    assert outcomes["call_0_5"].is_error
    assert "not found" in outcomes["call_0_5"].text


def test_real_omp_with_default_approval_would_have_run_the_refused_calls(tmp_path: Path) -> None:
    """The control: the trusted-local default (no approval flag) lets the same calls take effect."""
    binary = require_real_harness("omp")
    proof = make_proof_workspace(tmp_path)
    turns = [
        [
            ToolRequest("bash", {"command": "git push origin HEAD"}),
            ToolRequest("bash", {"command": f"touch {proof.workspace / 'shell-marker'}"}),
        ]
    ]
    harness = OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    task = harness_task(proof.workspace, "omp")
    environment = scrubbed_environment(
        binary,
        proof,
        {"HOME": "/unused", "PI_CODING_AGENT_DIR": "/unused", "GIT_TERMINAL_PROMPT": "0"},
    )
    command = list(harness.command_for(task))
    command.insert(command.index("-p"), "--model")
    command.insert(command.index("-p"), "anthropic/claude-sonnet-5-5")
    with LoopbackModelServer("pi-native", turns) as server:
        (proof.home / ".omp" / "agent" / "models.yml").write_text(
            f"providers:\n  anthropic:\n    baseUrl: {server.base_url}\n"
            f"    apiKey: {STUB_TOKEN}\n    transport: pi-native\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            (str(binary), *command[1:]),
            cwd=proof.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr[-500:]
    assert (proof.workspace / "shell-marker").exists()
    assert proof.remote_refs() != ""


_WORKSPACE_CODE_RUNNERS = ("pytest", "python", "uv ", "mypy", "ruff", "make", "npm", "bun", "node")


def test_no_allowed_command_runs_code_from_the_workspace() -> None:
    """A test runner or interpreter executes workspace files, which no deny entry can stop."""
    policy = yaml.safe_load(POLICY_ASSET.read_text(encoding="utf-8"))
    allowed = [rule["match"] for rule in policy["bash"]["patterns"] if rule["approval"] == "allow"]

    assert allowed  # local version control stays available
    assert all(match.startswith("git ") for match in allowed)
    assert not [
        match
        for match in allowed
        if any(match.startswith(name) for name in _WORKSPACE_CODE_RUNNERS)
    ]


def test_the_rationale_names_containment_not_the_allow_list_as_the_boundary() -> None:
    rationale = isolated_permission_mapping("omp").rationale

    assert "not the boundary" in rationale
    assert "Containment is the container" in rationale


def test_a_planted_hook_is_not_run_by_an_allowed_git_commit(tmp_path: Path) -> None:
    """`git commit` runs `.git/hooks/pre-commit` unless hooks are disabled out of band.

    The worker can write that file with its file tools, so the permission lists alone cannot
    keep it from running. The worker environment disables hooks with command-scoped config.
    """
    harness = _isolated_harness(tmp_path)
    git_environment = {
        key: value
        for key, value in harness.container_environment(harness_task(tmp_path, "omp"))
        if key.startswith("GIT_")
    }

    def commit(extra: dict[str, str]) -> Path:
        repository = tmp_path / ("hooked" if extra else "control")
        repository.mkdir()
        marker = repository / "hook-ran"
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        environment = {**base, **extra}
        for step in (("init", "-q"), ("config", "user.name", "w"), ("config", "user.email", "w@x")):
            subprocess.run(["git", *step], cwd=repository, env=environment, check=True)
        hook = repository / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        (repository / "file.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=repository, env=environment, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "work"], cwd=repository, env=environment, check=True
        )
        return marker

    assert commit({}).exists()  # the control: without the environment the hook runs
    assert not commit(git_environment).exists()
