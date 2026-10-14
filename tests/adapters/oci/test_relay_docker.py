"""The real relay container and a hardened worker on its own attempt network, on a real engine.

Skipped, by ``real_engine``, when no qualified container engine is reachable. Everything else
that goes wrong on an engine that is reachable fails the test.

The relay runs from the real packaged relay image and the real ``RelayManager`` under test-only
container and network names, so it never touches an operator's relay. The upstream is a
loopback recording stub reached through ``host.docker.internal``; the "worker" is a container
with the worker profile's exact hardening whose only job is to make requests and to try to
escape. No gateway is contacted and no token that exists outside this test is used.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from enginery.adapters.oci import image
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import hardening_arguments
from enginery.adapters.oci.relay import RELAY_TOKEN_MOUNT, RelayManager, RelayStatus
from enginery.domain.runner import RunnerProfile

from .relay_support import TOKEN, RecordingUpstream, write_token

UNREACHABLE_PUBLIC_ADDRESS = ("1.1.1.1", 80)

_WORKER_SCRIPT = """
import json, os, socket, sys, urllib.error, urllib.request

BASE = "http://enginery-relay:8080"
TOKEN = sys.argv[1]
UPSTREAM_PORT = int(sys.argv[2])


def call(method, path, document=None):
    data = None if document is None else json.dumps(document).encode()
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer enginery-relay",
            "X-Api-Key": "worker-placeholder",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return [response.status, response.read().decode()]
    except urllib.error.HTTPError as error:
        return [error.code, error.read().decode()]


def connect(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "connected"
    except Exception as error:
        return type(error).__name__


result = {
    "healthz": call("GET", "/healthz"),
    "messages": call("POST", "/v1/messages", {"model": "claude-sonnet-4-5", "messages": []}),
    "pi_stream": call(
        "POST", "/v1/pi/stream", {"modelId": "openai-codex/gpt-5.2", "context": {"messages": []}}
    ),
    "denied_model": call(
        "POST", "/v1/messages", {"model": "google-antigravity/gemini-3-pro", "messages": []}
    ),
    "denied_bare": call("POST", "/v1/pi/stream", {"modelId": "gpt-5", "context": {}}),
    "usage": call("GET", "/v1/usage"),
    "credentials": call("GET", "/v1/credentials/check"),
    "unknown": call("GET", "/v1/embeddings"),
    "models": call("GET", "/v1/models"),
    "direct_upstream": connect("host.docker.internal", UPSTREAM_PORT),
    "direct_public": connect(sys.argv[3], int(sys.argv[4])),
    "env_holds_token": any(TOKEN in value for value in os.environ.values()),
    "uid": os.getuid(),
}
print(json.dumps(result))
"""


@dataclass
class Stack:
    engine: OciEngine
    manager: RelayManager
    profile: RunnerProfile
    upstream: RecordingUpstream
    token_file: Path
    relay_image: str
    internal: str
    egress: str


@pytest.fixture(scope="module")
def relay_image(real_engine: OciEngine) -> str:
    return image.build_relay_image(real_engine).image_id


@pytest.fixture
def stack(real_engine: OciEngine, relay_image: str, tmp_path: Path) -> Iterator[Stack]:
    suffix = uuid.uuid4().hex[:10]
    prefix = f"enginery-test-internal-{suffix}"
    internal = f"{prefix}-{'a' * 24}"
    egress = f"enginery-test-egress-{suffix}"
    profile = RunnerProfile(network=prefix)
    manager = RelayManager(
        real_engine,
        profile=profile,
        egress_network=egress,
        container_name=f"enginery-relay-test-{suffix}",
    )
    upstream = RecordingUpstream(host="0.0.0.0")  # bound wide so the engine can reach it
    upstream.script.models_document = {
        "object": "list",
        "data": [
            {"id": "anthropic/claude-sonnet-4-5", "object": "model"},
            {"id": "google-antigravity/gemini-3-pro", "object": "model"},
            {"id": "openai-codex/gpt-5.2", "object": "model"},
            {"id": "openrouter/x", "object": "model"},
        ],
    }
    upstream.start()
    token_file = write_token(tmp_path / "gateway-token")
    try:
        yield Stack(
            real_engine, manager, profile, upstream, token_file, relay_image, internal, egress
        )
    finally:
        upstream.stop()
        with contextlib.suppress(Exception):
            manager.down()
        for network in (egress,):
            with contextlib.suppress(Exception):
                real_engine.run_cli(("network", "rm", network))


def _run_worker(stack: Stack) -> dict[str, object]:
    name = f"enginery-test-worker-{uuid.uuid4().hex[:10]}"
    host, port = UNREACHABLE_PUBLIC_ADDRESS
    stack.engine.create_attempt_network(stack.internal, operation_id="relay-docker-test")
    stack.manager.attach(stack.internal)
    try:
        completed = stack.engine.run_cli(
            (
                "run",
                "--rm",
                *hardening_arguments(stack.profile, network=stack.internal),
                "--name",
                name,
                "--entrypoint",
                "python3",
                stack.relay_image,
                "-c",
                _WORKER_SCRIPT,
                TOKEN,
                str(stack.upstream.port),
                host,
                str(port),
            ),
            timeout=180,
        )
    finally:
        stack.manager.detach(stack.internal)
        stack.engine.remove_attempt_network(stack.internal)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return dict(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_a_hardened_worker_reaches_models_only_through_the_relay(stack: Stack) -> None:
    status = stack.manager.up(
        relay_image=stack.relay_image,
        upstream=f"http://host.docker.internal:{stack.upstream.port}",
        token_file=stack.token_file,
    )
    assert status.healthy, status.detail
    assert status.running and status.image_id == stack.relay_image
    assert status.networks == (stack.egress,)  # no worker network while no attempt runs

    result = _run_worker(stack)

    assert result["uid"] == 65532
    assert result["env_holds_token"] is False
    healthz, messages, pi_stream = result["healthz"], result["messages"], result["pi_stream"]
    assert isinstance(healthz, list) and healthz[0] == 200
    assert isinstance(messages, list) and messages[0] == 200
    assert isinstance(pi_stream, list) and pi_stream[0] == 200
    for name in ("denied_model", "denied_bare", "usage", "credentials"):
        refused = result[name]
        assert isinstance(refused, list) and refused[0] == 403, name
    unknown = result["unknown"]
    assert isinstance(unknown, list) and unknown[0] == 404
    models = result["models"]
    assert isinstance(models, list) and models[0] == 200
    assert [row["id"] for row in json.loads(models[1])["data"]] == [
        "anthropic/claude-sonnet-4-5",
        "openai-codex/gpt-5.2",
    ]

    # What reached the gateway: exactly the three allowed requests, with the real bearer, no
    # worker credential, and the bare claude id canonicalized.
    forwarded = stack.upstream.forwarded()
    assert [(seen.method, seen.path) for seen in forwarded] == [
        ("POST", "/v1/messages"),
        ("POST", "/v1/pi/stream"),
        ("GET", "/v1/models"),
    ]
    for seen in forwarded:
        assert seen.headers["authorization"] == [f"Bearer {TOKEN}"]
        assert "x-api-key" not in seen.headers
    assert forwarded[0].json() == {"model": "anthropic/claude-sonnet-4-5", "messages": []}

    # The worker's own network offers no other route out.
    assert result["direct_upstream"] != "connected"
    assert result["direct_public"] != "connected"


def test_the_relay_container_is_hardened_holds_only_the_token_and_leaks_nothing(
    stack: Stack,
) -> None:
    stack.manager.up(
        relay_image=stack.relay_image,
        upstream=f"http://host.docker.internal:{stack.upstream.port}",
        token_file=stack.token_file,
    )

    container = stack.engine.container_inspect(stack.manager.container_name)

    host = container["HostConfig"]
    config = container["Config"]
    assert isinstance(host, dict) and isinstance(config, dict)
    # Non-root on every engine: the worker uid, the token owner on plain Docker, or mapped root
    # (no host privilege) on a rootless engine; the relay's own expectation is the oracle.
    assert config["User"] == stack.manager._expected_user()
    assert config["User"] != "0:0" or stack.engine.identity().rootless
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["Privileged"] is False
    assert host["Binds"] == [f"{stack.token_file}:{RELAY_TOKEN_MOUNT}:ro"]
    assert host["PortBindings"] in ({}, None)
    assert not host.get("Devices")
    serialized = json.dumps(container)
    assert TOKEN not in serialized
    assert stack.manager.status().healthy


def test_up_again_is_idempotent_and_down_removes_the_relay(stack: Stack) -> None:
    def up() -> RelayStatus:
        return stack.manager.up(
            relay_image=stack.relay_image,
            upstream=f"http://host.docker.internal:{stack.upstream.port}",
            token_file=stack.token_file,
        )

    first = up()
    created = stack.engine.container_inspect(stack.manager.container_name)["Id"]

    second = up()

    assert first.healthy and second.healthy
    assert stack.engine.container_inspect(stack.manager.container_name)["Id"] == created
    assert stack.engine.network_info(stack.egress) is not None
    assert stack.manager.down().present is False
    assert stack.manager.status().present is False
    # ``relay down`` also removes the egress network it created, and again is idempotent.
    assert stack.engine.network_info(stack.egress) is None
    assert stack.manager.down().present is False
    assert stack.engine.network_info(stack.egress) is None
