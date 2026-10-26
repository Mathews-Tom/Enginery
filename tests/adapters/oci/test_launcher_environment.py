"""The host process that launches an isolated validation holds no ambient credential."""

from __future__ import annotations

import json
import sys

import pytest

from enginery.adapters.oci.validation import _run_launcher
from enginery.adapters.runner_launch import worker_environment
from enginery.application.worker_environment import LAUNCHER_KEYS


def test_the_validation_launcher_process_receives_only_the_launcher_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "GH_TOKEN": "ambient",
        "GITHUB_TOKEN": "ambient",
        "SSH_AUTH_SOCK": "/tmp/ambient.sock",
        "AWS_SECRET_ACCESS_KEY": "ambient",
        "ANTHROPIC_API_KEY": "ambient",
        "ENGINERY_AMBIENT_MARKER": "ambient",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/engine.sock")

    completed = _run_launcher(
        (sys.executable, "-c", "import json, os; print(json.dumps(sorted(os.environ)))")
    )

    names = set(json.loads(completed.stdout))
    assert names <= set(LAUNCHER_KEYS) | {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert names.isdisjoint(secrets)
    assert "DOCKER_HOST" in names, "the launcher still reaches the engine it is configured for"


def test_worker_path_preserves_system_tools_before_the_checkout_runtime() -> None:
    environment = dict(worker_environment(()))
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin:/opt/enginery/runtime/bin"
