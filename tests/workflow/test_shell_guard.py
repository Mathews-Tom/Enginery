"""Behavior tests for the launcher-aware shell-interpreter guard."""

from __future__ import annotations

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.domain.workflow.check import CheckDeclaration
from enginery.domain.workflow.shell_guard import reject_shell_invocation

REJECTED = [
    # direct, path, case, and Windows forms
    ("bash", "-c", "x"),
    ("/usr/bin/zsh", "-c", "x"),
    ("BASH.EXE", "-c", "x"),
    ("C:\\Windows\\System32\\cmd.exe", "/c", "x"),
    # simple launchers
    ("env", "bash", "-c", "x"),
    ("/usr/bin/env", "-i", "FOO=1", "BAR=2", "sh", "-c", "x"),
    ("env", "-u", "HOME", "bash", "-c", "x"),
    ("env", "-S", "bash -c x"),
    ("env", "--split-string=bash -c x"),
    ("busybox", "sh", "-c", "x"),
    ("nice", "-n", "5", "dash", "-c", "x"),
    ("nice", "-5", "bash"),
    ("nohup", "/bin/bash", "-c", "x"),
    ("exec", "pwsh", "-Command", "x"),
    ("timeout", "10", "sh", "-c", "x"),
    ("timeout", "-k", "5", "10", "bash"),
    ("sudo", "-u", "root", "sh", "-c", "x"),
    ("time", "-p", "bash"),
    ("stdbuf", "-o0", "bash"),
    # package-runner and multi-word launchers
    ("uv", "run", "bash", "-c", "x"),
    ("uv", "run", "--python", "3.12", "--with", "x", "sh", "-c", "x"),
    ("uv", "tool", "run", "bash"),
    ("uvx", "bash"),
    ("uvx", "--from", "somepkg", "bash", "-c", "x"),
    ("poetry", "run", "bash", "-c", "x"),
    ("pipenv", "run", "sh", "-c", "x"),
    ("npx", "sh", "-c", "x"),
    ("npx", "-y", "-p", "pkg", "bash"),
    ("pnpm", "exec", "bash", "-c", "x"),
    ("pnpm", "exec", "--filter", "web", "zsh"),
    ("npm", "exec", "--", "bash"),
    # operand-taking launchers
    ("flock", "/tmp/lock", "bash", "-c", "x"),
    ("flock", "-w", "5", "/tmp/lock", "sh"),
    ("taskset", "0x1", "bash"),
    ("taskset", "-c", "0,1", "sh"),
    ("caffeinate", "-d", "bash"),
    ("caffeinate", "-t", "60", "sh"),
    ("sandbox-exec", "-f", "profile.sb", "bash"),
    # options that themselves run a shell
    ("sudo", "-s"),
    ("sudo", "-i"),
    ("flock", "/tmp/lock", "-c", "echo hi"),
    ("npx", "-c", "echo hi"),
    ("npm", "exec", "--call", "echo hi"),
    # nested launchers
    ("env", "nice", "uv", "run", "bash", "-c", "x"),
    ("nohup", "timeout", "10", "env", "FOO=1", "sudo", "-u", "root", "sh"),
    ("caffeinate", "-i", "uvx", "--from", "pkg", "bash"),
]

ACCEPTED = [
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "python", "scripts/full_system_gate.py", "--stages", "2"),
    ("uv", "run", "enginery", "doctor"),
    ("uv", "sync", "--all-extras"),
    ("env", "FOO=1", "pytest", "-q"),
    ("nice", "-n", "5", "python", "-m", "pytest"),
    ("/usr/bin/python3", "script.py"),
    ("timeout", "600", "go", "test", "./cmd"),
    ("nice", "pytest", "tests/sh"),
    ("timeout", "600", "uv", "run", "pytest", "-k", "sh"),
    ("env", "PYTHONPATH=src", "pytest", "--shell", "zsh"),
    ("time", "make", "-C", "build/sh"),
    ("flock", "/tmp/lock", "make", "bash"),
    ("npx", "prettier", "--check", "bash"),
    ("pnpm", "exec", "vitest", "run", "sh"),
    ("python", "-c", "import subprocess"),
    ("env",),
]


@pytest.mark.parametrize("argv", REJECTED, ids=lambda argv: " ".join(argv))
def test_a_command_that_launches_a_shell_is_rejected(argv: tuple[str, ...]) -> None:
    with pytest.raises(InvalidInputError, match="shell interpreter"):
        reject_shell_invocation(argv)


@pytest.mark.parametrize("argv", ACCEPTED, ids=lambda argv: " ".join(argv))
def test_a_command_that_only_mentions_a_shell_name_is_accepted(argv: tuple[str, ...]) -> None:
    reject_shell_invocation(argv)


@pytest.mark.parametrize("argv", REJECTED[:6] + REJECTED[19:22])
def test_the_same_guard_applies_to_declared_check_commands(argv: tuple[str, ...]) -> None:
    with pytest.raises(InvalidInputError, match="shell interpreter"):
        CheckDeclaration.from_mapping(
            {
                "id": "unit.lint",
                "subject": "workspace_head",
                "command": {"source": "declared", "argv": list(argv)},
                "on_fail": "stop",
                "on_indeterminate": "stop",
            }
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("timeout", "--unheard-of", "10", "bash"),
        ("nice", "--mystery", "bash"),
        ("uv", "run", "--made-up-flag", "bash"),
        ("env", "--chdir"),
    ],
)
def test_an_unknown_option_on_a_known_launcher_fails_closed(argv: tuple[str, ...]) -> None:
    with pytest.raises(InvalidInputError, match=r"unrecognized option|needs a value"):
        reject_shell_invocation(argv)


def test_launchers_nested_beyond_the_resolution_limit_are_rejected() -> None:
    argv = ("env",) * 40 + ("pytest",)

    with pytest.raises(InvalidInputError, match="too deeply"):
        reject_shell_invocation(argv)
