"""What an isolated worker is given to reach a model: only the relay URL and a placeholder."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.runner_context import harness_task, isolated_context

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.adapters.runner_launch import (
    OMP_MODELS_PATH,
    relay_claude_environment,
    relay_omp_seed_files,
)
from enginery.domain.runner import (
    DEFAULT_ENV_ALLOWLIST,
    RELAY_DUMMY_CREDENTIAL,
    LaunchSpec,
    RelayEndpoint,
)
from enginery.ledger.artifact_store import ArtifactStore

RELAY_URL = "http://enginery-relay:8080"


def _identity(output: str) -> Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]:
    """A probe runner that answers the in-image identity command like the tested binary."""

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    return run


def _claude(tmp_path: Path, *, relay: bool) -> ClaudeCodeHarness:
    return ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="claude-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        command_runner=_identity("2.1.296 (Claude Code)\n"),
        runner=isolated_context(tmp_path / "receipts", relay=relay),
    )


def _omp(tmp_path: Path, *, relay: bool) -> OmpHarness:
    return OmpHarness(
        OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
        ArtifactStore(tmp_path / "artifacts"),
        command_runner=_identity("omp v18.10.3\n\nUSAGE\n"),
        runner=isolated_context(tmp_path / "receipts", relay=relay),
    )


def _spec_from(command: tuple[str, ...]) -> LaunchSpec:
    return LaunchSpec.from_state(json.loads(command[command.index("--spec") + 1]))


def test_claude_code_with_a_relay_gets_the_relay_endpoint_and_the_placeholder(
    tmp_path: Path,
) -> None:
    harness = _claude(tmp_path, relay=True)
    task = harness_task(tmp_path / "workspace", "claude-code")

    environment = dict(harness.container_environment(task))

    assert environment["ANTHROPIC_BASE_URL"] == RELAY_URL
    assert environment["ANTHROPIC_AUTH_TOKEN"] == RELAY_DUMMY_CREDENTIAL
    assert environment["DISABLE_TELEMETRY"] == "1"
    assert environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert environment["DISABLE_AUTOUPDATER"] == "1"
    assert "ANTHROPIC_API_KEY" not in environment
    assert set(environment) <= set(DEFAULT_ENV_ALLOWLIST)


def test_claude_code_without_a_relay_gets_no_model_endpoint_or_credential(tmp_path: Path) -> None:
    harness = _claude(tmp_path, relay=False)
    task = harness_task(tmp_path / "workspace", "claude-code")

    environment = dict(harness.container_environment(task))

    assert not {key for key in environment if key.startswith("ANTHROPIC_")}
    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))
    assert spec.seed_files == ()


def test_claude_code_carries_no_seed_file_with_a_relay(tmp_path: Path) -> None:
    harness = _claude(tmp_path, relay=True)
    task = harness_task(tmp_path / "workspace", "claude-code")

    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))

    assert spec.seed_files == ()
    assert dict(spec.env)["ANTHROPIC_BASE_URL"] == RELAY_URL


def test_omp_with_a_relay_is_seeded_with_a_pi_native_models_file(tmp_path: Path) -> None:
    harness = _omp(tmp_path, relay=True)
    task = harness_task(tmp_path / "workspace", "omp")

    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))

    assert [path for path, _ in spec.seed_files] == [OMP_MODELS_PATH]
    assert OMP_MODELS_PATH == "/home/worker/.omp/agent/models.yml"
    assert dict(spec.env)["PI_CODING_AGENT_DIR"] == "/home/worker/.omp/agent"
    content = spec.seed_files[0][1]
    assert content == (
        "providers:\n"
        "  anthropic:\n"
        f"    baseUrl: {RELAY_URL}\n"
        f"    apiKey: {RELAY_DUMMY_CREDENTIAL}\n"
        "    transport: pi-native\n"
        "  openai-codex:\n"
        f"    baseUrl: {RELAY_URL}\n"
        f"    apiKey: {RELAY_DUMMY_CREDENTIAL}\n"
        "    transport: pi-native\n"
    )
    assert not {key for key in dict(spec.env) if key.startswith(("ANTHROPIC_", "OPENAI_"))}


def test_omp_without_a_relay_has_no_seed_and_no_model_endpoint(tmp_path: Path) -> None:
    harness = _omp(tmp_path, relay=False)
    task = harness_task(tmp_path / "workspace", "omp")

    spec = _spec_from(harness.supervised_command(task, result_path=tmp_path / "out" / "r.json"))

    assert spec.seed_files == ()


def test_the_helpers_grant_nothing_without_a_relay_context(tmp_path: Path) -> None:
    context = isolated_context(tmp_path / "receipts", relay=False)

    assert relay_claude_environment(context) == ()
    assert relay_omp_seed_files(context) == ()


def test_nothing_in_the_launch_spec_is_a_real_credential(tmp_path: Path) -> None:
    for harness_name, harness in (
        ("claude-code", _claude(tmp_path, relay=True)),
        ("omp", _omp(tmp_path, relay=True)),
    ):
        task = harness_task(tmp_path / "workspace", harness_name)
        command = harness.supervised_command(task, result_path=tmp_path / "out" / "r.json")
        encoded = command[command.index("--spec") + 1]
        assert RELAY_DUMMY_CREDENTIAL in encoded
        assert "Bearer" not in encoded
        assert "sk-" not in encoded


@pytest.mark.parametrize(
    "url",
    [
        "http://enginery-relay",
        "https://enginery-relay:8080",
        "http://enginery-relay:8080/",
        "http://user:pw@enginery-relay:8080",
        "http://enginery-relay:8080/v1",
        "",
    ],
)
def test_a_relay_endpoint_url_must_be_a_bare_http_origin(url: str) -> None:
    with pytest.raises(Exception, match="relay url"):
        RelayEndpoint(url=url)


def test_a_worker_is_only_ever_given_the_fixed_placeholder() -> None:
    with pytest.raises(Exception, match="placeholder"):
        RelayEndpoint(dummy_credential="sk-ant-real-looking")
