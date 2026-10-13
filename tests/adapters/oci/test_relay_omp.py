"""The real ``omp`` binary, seeded exactly as an isolated worker is, talking to the real relay.

Skipped when ``omp`` is not installed. No model is contacted: the relay's upstream is a loopback
recording stub that answers every model request with an error, and omp runs with an empty home
and no credential other than the placeholder the seeded ``models.yml`` names.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.support.runner_context import IMAGE_ID

from enginery.adapters.runner_launch import OMP_MODELS_PATH, relay_omp_seed_files
from enginery.domain.runner import (
    MODEL_ACCESS_RELAY,
    WORKER_HOME,
    RelayEndpoint,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
)

from .relay_support import TOKEN, RecordingUpstream, running_relay, write_token

MODEL = "anthropic/claude-sonnet-4-5"


@pytest.mark.skipif(shutil.which("omp") is None, reason="omp is not installed")
def test_omp_posts_pi_stream_with_the_provider_qualified_model_id_to_the_relay(
    tmp_path: Path,
) -> None:
    upstream = RecordingUpstream()
    # A client error is not retried, so omp gives up at once instead of backing off.
    upstream.script.status = 400
    upstream.script.body = b'{"error":{"type":"invalid_request_error","message":"stub"}}'
    upstream.start()
    token_file = write_token(tmp_path / "gateway-token")
    try:
        with running_relay(upstream.url, token_file) as relay:
            endpoint = RelayEndpoint(url=f"http://127.0.0.1:{relay.port}")
            profile = RunnerProfile()
            context = RunnerContext(
                selection=RunnerSelection.isolated(
                    image=IMAGE_ID, profile=profile, model_access=MODEL_ACCESS_RELAY
                ),
                profile=profile,
                receipt_root=str(tmp_path / "receipts"),
                relay=endpoint,
            )
            ((seed_path, models_yml),) = relay_omp_seed_files(context)
            assert seed_path == OMP_MODELS_PATH
            home = tmp_path / "home"
            agent_dir = home / ".omp" / "agent"
            agent_dir.mkdir(parents=True)
            assert seed_path == f"{WORKER_HOME}/.omp/agent/models.yml"
            (agent_dir / "models.yml").write_text(models_yml, encoding="utf-8")
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(home),
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_CONFIG_DIR": str(home / ".omp"),
                "TERM": "dumb",
            }
            subprocess.run(
                [
                    "omp",
                    "--mode=json",
                    "--no-session",
                    "--max-time",
                    "40",
                    "--model",
                    MODEL,
                    "-p",
                    "say hello",
                ],
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

    streams = [seen for seen in upstream.forwarded() if seen.path == "/v1/pi/stream"]
    assert streams, "omp never posted to the relay's pi-native route"
    assert streams[0].method == "POST"
    document = streams[0].json()
    assert isinstance(document, dict) and document["modelId"] == MODEL
    # The relay replaced omp's placeholder with the gateway bearer.
    assert streams[0].headers["authorization"] == [f"Bearer {TOKEN}"]
    # Whatever else omp asks the relay for is refused there and never reaches the gateway.
    assert {seen.path for seen in upstream.forwarded()} <= {"/v1/pi/stream", "/v1/models"}
    assert "enginery-relay" not in relay_log
    assert TOKEN not in relay_log
