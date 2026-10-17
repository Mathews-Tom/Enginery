"""Operator-owned behavioral policy for real request-building journeys."""

from __future__ import annotations

import subprocess
from pathlib import Path

from enginery.application.behavioral_checks import BehavioralCheck, checks_from_state


def fixture_behavioral_checks() -> tuple[BehavioralCheck, ...]:
    """A nonempty operator contract for in-process workflow adapter fixtures."""
    return checks_from_state(
        [
            {
                "name": "fixture-content",
                "observer": "command",
                "argv": ["cat", "README"],
                "fault_classes": ["constant_result"],
                "assertions": [{"kind": "stdout", "contains": "fixture"}],
            }
        ]
    )


def declare_behavioral_policy(repository: Path) -> str:
    policy = repository / ".enginery" / "project.toml"
    policy.parent.mkdir(exist_ok=True)
    if not policy.exists():
        policy.write_text("schema_version = 1\n", encoding="utf-8")
    with policy.open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[[acceptance.checks]]\nname = "fixture-content"\nobserver = "command"\n'
            'argv = ["cat", "README"]\nfault_classes = ["constant_result"]\n'
            '[[acceptance.checks.assertions]]\nkind = "stdout"\ncontains = "fixture"\n'
        )
    subprocess.run(
        ["git", "add", ".enginery/project.toml"], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "declare behavioral policy"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
