"""The relay in front of the real model gateway: model listing and refusals only.

Runs only when ``ENGINERY_GATEWAY_TOKEN_FILE`` names a gateway token file (mode 0600, yours);
``ENGINERY_GATEWAY_URL`` names the gateway and defaults to ``http://127.0.0.1:47400``. The only
request that reaches the gateway is ``GET /v1/models``; every other request in this module is
one the relay must refuse before it gets there. No inference is requested, and the token is
never printed: assertions report counts only.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from enginery.adapters.oci.relay import check_token_file

from .relay_support import RelayProcess, running_relay

TOKEN_FILE_ENVIRONMENT = "ENGINERY_GATEWAY_TOKEN_FILE"
GATEWAY_URL_ENVIRONMENT = "ENGINERY_GATEWAY_URL"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:47400"
PERMITTED_PREFIXES = ("anthropic/", "openai-codex/")


@pytest.fixture(scope="module")
def token_file() -> Path:
    named = os.environ.get(TOKEN_FILE_ENVIRONMENT)
    if not named:
        pytest.skip(f"{TOKEN_FILE_ENVIRONMENT} is not set")
    return check_token_file(Path(named))


@pytest.fixture(scope="module")
def gateway_url() -> str:
    return os.environ.get(GATEWAY_URL_ENVIRONMENT, DEFAULT_GATEWAY_URL)


@pytest.fixture(scope="module")
def relay(token_file: Path, gateway_url: str) -> Iterator[RelayProcess]:
    with running_relay(gateway_url, token_file) as process:
        yield process
    token = token_file.read_text(encoding="utf-8").strip()
    assert token not in process.stderr_text, "the relay logged the gateway token"


def _direct_model_ids(gateway_url: str, token_file: Path) -> list[str]:
    parts = urllib.parse.urlsplit(gateway_url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=30)
    try:
        connection.request(
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {token_file.read_text(encoding='utf-8').strip()}"},
        )
        response = connection.getresponse()
        body = response.read()
    except OSError:
        pytest.skip("the gateway is not reachable")
    finally:
        connection.close()
    assert response.status == 200, f"gateway answered {response.status} to the model list"
    return [str(row["id"]) for row in json.loads(body)["data"]]


def test_the_model_list_through_the_relay_is_the_permitted_subset_of_the_gateways(
    relay: RelayProcess, token_file: Path, gateway_url: str
) -> None:
    gateway_ids = _direct_model_ids(gateway_url, token_file)

    status, headers, body = relay.request("GET", "/v1/models")

    assert status == 200, f"the relay answered {status} to the model list"
    assert headers["Content-Type"] == "application/json"
    relayed = [str(row["id"]) for row in json.loads(body)["data"]]
    expected = [model for model in gateway_ids if model.startswith(PERMITTED_PREFIXES)]
    assert relayed == expected
    assert relayed, "the gateway lists no model of a permitted provider"
    assert len(relayed) <= len(gateway_ids)
    assert all(model.startswith(PERMITTED_PREFIXES) for model in relayed)
    print(f"gateway models: {len(gateway_ids)}; through the relay: {len(relayed)}")


@pytest.mark.parametrize("path", ["/v1/credentials/check", "/v1/usage"])
def test_the_credential_and_usage_routes_are_refused_by_the_relay(
    relay: RelayProcess, path: str
) -> None:
    assert relay.request("GET", path)[0] == 403


@pytest.mark.parametrize(
    "model", ["google-antigravity/gemini-3-pro", "openai/gpt-5", "gpt-5", "openrouter/x/y"]
)
def test_a_model_outside_the_allowlist_is_refused_by_the_relay_before_the_gateway(
    relay: RelayProcess, model: str
) -> None:
    for route, document in (
        ("/v1/messages", {"model": model, "messages": []}),
        ("/v1/pi/stream", {"modelId": model, "context": {"messages": []}}),
    ):
        status, _, _ = relay.post_json(route, document)
        assert status == 403, route
