"""The isolated Claude Code permission mapping: declared flags and a real-binary refusal proof."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.support.loopback_model import STUB_TOKEN, LoopbackModelServer, ToolRequest
from tests.support.real_harness import (
    ProofWorkspace,
    RealHarness,
    make_proof_workspace,
    require_real_target,
)
from tests.support.runner_context import harness_task, isolated_context

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    isolated_permission_mapping,
    trusted_local_permission_mapping,
)
from enginery.ledger.artifact_store import ArtifactStore


def _isolated_harness(tmp_path: Path) -> ClaudeCodeHarness:
    return ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="claude-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        runner=isolated_context(tmp_path / "receipts"),
    )


def _values_after(arguments: tuple[str, ...], flag: str) -> tuple[str, ...]:
    """The values of one variadic flag: everything up to the next ``--`` option."""
    start = arguments.index(flag) + 1
    values: list[str] = []
    for argument in arguments[start:]:
        if argument.startswith("--"):
            break
        values.append(argument)
    return tuple(values)


def test_isolated_mapping_is_not_a_bypass_and_scopes_the_tools() -> None:
    mapping = isolated_permission_mapping("claude-code")
    arguments = mapping.arguments

    assert mapping.unconditional_bypass is False
    assert "bypassPermissions" not in arguments
    assert "--dangerously-skip-permissions" not in arguments
    assert arguments[arguments.index("--permission-mode") + 1] == "dontAsk"
    assert arguments[arguments.index("--permission-prompts") + 1] == "none"
    assert "--restricted" in arguments
    allowed = _values_after(arguments, "--allowedTools")
    denied = _values_after(arguments, "--disallowedTools")
    assert {"Read", "Edit", "Write", "Glob", "Grep"} <= set(allowed)
    assert "Bash(git commit:*)" in allowed
    assert "Bash" not in allowed  # shell access is only ever granted per command pattern
    assert {"Bash(git push:*)", "WebFetch", "WebSearch"} <= set(denied)
    assert not set(allowed) & set(denied)


def test_trusted_local_mapping_is_the_declared_bypass() -> None:
    mapping = trusted_local_permission_mapping("claude-code")

    assert mapping.unconditional_bypass is True
    assert mapping.arguments == ("--permission-mode", "bypassPermissions")


def test_trusted_local_command_keeps_the_existing_arguments(tmp_path: Path) -> None:
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="claude-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
    )
    task = harness_task(tmp_path, "claude-code")

    command = harness.command_for(task)

    assert command[0] == "claude"
    assert command[1] == "-p"
    assert command[3:] == (
        "--output-format",
        "stream-json",
        "--verbose",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--session-id",
        command[command.index("--session-id") + 1],
        "--strict-mcp-config",
        "--permission-mode",
        "bypassPermissions",
    )


def test_isolated_command_carries_the_mapping_verbatim_and_no_bypass(tmp_path: Path) -> None:
    harness = _isolated_harness(tmp_path)

    command = harness.command_for(harness_task(tmp_path, "claude-code"))

    mapping = isolated_permission_mapping("claude-code").arguments
    start = command.index(mapping[0])
    assert command[start : start + len(mapping)] == mapping
    assert "bypassPermissions" not in command
    assert "--strict-mcp-config" in command
    assert "--cwd" not in command  # the container's working directory is the workspace mount


@dataclass(frozen=True, slots=True)
class _Outcome:
    is_error: bool
    text: str


def _tool_outcomes(stream_json: str) -> dict[str, _Outcome]:
    """Map each tool use id to what the harness itself reported back to the model."""
    outcomes: dict[str, _Outcome] = {}
    for line in stream_json.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "user":
            continue
        for block in event["message"]["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                outcomes[block["tool_use_id"]] = _Outcome(bool(block.get("is_error")), text)
    return outcomes


def _run_claude(
    harness: ClaudeCodeHarness,
    proof: ProofWorkspace,
    real: RealHarness,
    server: LoopbackModelServer,
) -> str:
    """Run the adapter's isolated argv on the real claude the catalog tests, against the stub."""
    task = harness_task(proof.workspace, "claude-code")
    completed = real.run(
        harness.command_for(task),
        proof,
        dict(harness.container_environment(task)),
        extra_environment={
            "ANTHROPIC_BASE_URL": real.stub_url(server),
            "ANTHROPIC_AUTH_TOKEN": STUB_TOKEN,
        },
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    return completed.stdout


def test_real_claude_refuses_disallowed_tool_calls_and_runs_allowed_ones(tmp_path: Path) -> None:
    real = require_real_target("claude-code")
    proof = make_proof_workspace(tmp_path)
    allowed_file = real.view(proof, proof.workspace / "allowed.txt")
    seen_workspace = real.view(proof, proof.workspace)
    turns = [
        [
            ToolRequest("Write", {"file_path": str(allowed_file), "content": "allowed\n"}),
            ToolRequest("Bash", {"command": "git push origin HEAD", "description": "publish"}),
            ToolRequest(
                "Write",
                {"file_path": str(proof.outside / "escape.txt"), "content": "escaped\n"},
            ),
            ToolRequest(
                "Bash",
                {"command": f"touch {seen_workspace / 'shell-marker'}", "description": "touch"},
            ),
            ToolRequest("Read", {"file_path": str(proof.outside / "secret.txt")}),
            ToolRequest("WebFetch", {"url": "http://127.0.0.1:9/x", "prompt": "fetch"}),
        ],
        [ToolRequest("Bash", {"command": "git add allowed.txt", "description": "stage"})],
        [ToolRequest("Bash", {"command": "git commit -m 'add allowed file'", "description": "c"})],
    ]
    harness = _isolated_harness(tmp_path)

    with real.stub("anthropic", turns) as server:
        output = _run_claude(harness, proof, real, server)

    outcomes = _tool_outcomes(output)
    # Allowed: a write inside the workspace and the two local git steps.
    assert not outcomes["toolu_0_0"].is_error
    assert (proof.workspace / "allowed.txt").read_text(encoding="utf-8") == "allowed\n"
    assert not outcomes["toolu_1_0"].is_error
    assert not outcomes["toolu_2_0"].is_error
    assert proof.git("log", "-1", "--format=%s") == "add allowed file\n"
    assert proof.git("show", "--name-only", "--format=", "HEAD") == "allowed.txt\n"
    # Refused: publishing, a write and a read outside the workspace, an unlisted shell command,
    # and a tool the harness was not given. None of them left a side effect.
    assert outcomes["toolu_0_1"].is_error
    assert "denied" in outcomes["toolu_0_1"].text
    assert proof.remote_refs() == ""
    assert outcomes["toolu_0_2"].is_error
    assert not (proof.outside / "escape.txt").exists()
    assert outcomes["toolu_0_3"].is_error
    assert not (proof.workspace / "shell-marker").exists()
    assert outcomes["toolu_0_4"].is_error
    assert "outside the workspace\n" not in outcomes["toolu_0_4"].text
    assert outcomes["toolu_0_5"].is_error
    assert "No such tool" in outcomes["toolu_0_5"].text


def test_real_claude_bypass_would_have_run_the_refused_calls(tmp_path: Path) -> None:
    """The control: with the trusted-local bypass the same calls take effect, so the refusals
    above come from the mapping and not from the stub, the workspace or the environment."""
    real = require_real_target("claude-code")
    proof = make_proof_workspace(tmp_path)
    turns = [
        [
            ToolRequest("Bash", {"command": "git push origin HEAD", "description": "publish"}),
            ToolRequest(
                "Write",
                {"file_path": str(proof.outside / "escape.txt"), "content": "escaped\n"},
            ),
        ]
    ]
    harness = ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="claude-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        runner=None,
    )
    task = harness_task(proof.workspace, "claude-code")
    with real.stub("anthropic", turns) as server:
        completed = real.run(
            harness.command_for(task),
            proof,
            {
                "HOME": "/home/worker",
                "CLAUDE_CONFIG_DIR": "/home/worker/.claude",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
            extra_environment={
                "ANTHROPIC_BASE_URL": real.stub_url(server),
                "ANTHROPIC_AUTH_TOKEN": STUB_TOKEN,
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )

    assert completed.returncode == 0, completed.stderr[-500:]
    assert (proof.outside / "escape.txt").exists()
    assert proof.remote_refs() != ""


def test_isolated_harness_refuses_a_bypass_override(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="unconditional permission bypass"):
        ClaudeCodeHarness(
            ClaudeCodeAdapterConfig(credential_reference="claude-auth-profile:default"),
            ArtifactStore(tmp_path / "artifacts"),
            permissions=trusted_local_permission_mapping("claude-code"),
            runner=isolated_context(tmp_path / "receipts"),
        )


_WORKSPACE_CODE_RUNNERS = ("pytest", "python", "uv ", "mypy", "ruff", "make", "npm", "bun", "node")


def test_no_allowed_command_runs_code_from_the_workspace() -> None:
    """A test runner or interpreter executes workspace files, which no deny entry can stop."""
    arguments = isolated_permission_mapping("claude-code").arguments
    allowed_shell = [
        entry for entry in _values_after(arguments, "--allowedTools") if entry.startswith("Bash(")
    ]

    assert allowed_shell  # local version control stays available
    assert all(entry.startswith("Bash(git ") for entry in allowed_shell)
    assert not [
        entry
        for entry in allowed_shell
        if any(entry.startswith(f"Bash({name}") for name in _WORKSPACE_CODE_RUNNERS)
    ]


def test_the_rationale_names_containment_not_the_allow_list_as_the_boundary() -> None:
    rationale = isolated_permission_mapping("claude-code").rationale

    assert "not the boundary" in rationale
    assert "Containment is the container" in rationale
