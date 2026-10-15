"""The real ``claude`` binary, given exactly the isolated worker's model environment, via the relay.

Skipped when ``claude`` is not installed. No model is contacted: the relay's upstream is a
loopback recording stub that answers every model request with a client error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.support.runner_context import IMAGE_ID

from enginery.adapters.runner_launch import relay_claude_environment
from enginery.domain.runner import (
    MODEL_ACCESS_RELAY,
    RelayEndpoint,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
)

from .relay_support import TOKEN, RecordingUpstream, running_relay, write_token


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not installed")
def test_claude_code_posts_messages_with_a_canonical_model_id_and_the_gateway_bearer(
    tmp_path: Path,
) -> None:
    upstream = RecordingUpstream()
    upstream.script.status = 400
    upstream.script.body = b'{"type":"error","error":{"type":"invalid_request_error"}}'
    upstream.start()
    token_file = write_token(tmp_path / "gateway-token")
    try:
        with running_relay(upstream.url, token_file) as relay:
            profile = RunnerProfile()
            context = RunnerContext(
                selection=RunnerSelection.isolated(
                    image=IMAGE_ID, profile=profile, model_access=MODEL_ACCESS_RELAY
                ),
                profile=profile,
                receipt_root=str(tmp_path / "receipts"),
                relay=RelayEndpoint(url=f"http://127.0.0.1:{relay.port}"),
            )
            home = tmp_path / "home"
            (home / ".claude").mkdir(parents=True)
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "TERM": "dumb",
                **dict(relay_claude_environment(context)),
            }
            subprocess.run(
                ["claude", "-p", "say hello", "--model", "claude-sonnet-4-5"],
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            relay_log = relay.stop()
    finally:
        upstream.stop()

    forwarded = upstream.forwarded()
    assert forwarded, "claude never posted to the relay"
    assert {seen.path.split("?")[0] for seen in forwarded} == {"/v1/messages"}
    for seen in forwarded:
        document = seen.json()
        assert isinstance(document, dict) and document["model"] == "anthropic/claude-sonnet-4-5"
        assert seen.headers["authorization"] == [f"Bearer {TOKEN}"]
        assert "x-api-key" not in seen.headers
    assert TOKEN not in relay_log
