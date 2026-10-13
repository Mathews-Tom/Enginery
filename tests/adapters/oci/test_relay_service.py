"""The real ``relay.py`` as a subprocess, in front of a recording loopback upstream.

Every refusal is asserted twice: by the response the worker sees and by the upstream's own
request log, which must show that nothing reached the gateway.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from .relay_support import (
    RELAY_SCRIPT,
    TOKEN,
    RecordingUpstream,
    RelayProcess,
    running_relay,
    write_token,
)

CLAUDE_BODY = {"model": "claude-sonnet-4-5", "max_tokens": 16, "messages": []}


@pytest.fixture
def upstream() -> Iterator[RecordingUpstream]:
    server = RecordingUpstream()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    return write_token(tmp_path / "gateway-token")


@pytest.fixture
def relay(upstream: RecordingUpstream, token_file: Path) -> Iterator[RelayProcess]:
    with running_relay(upstream.url, token_file) as process:
        yield process


# --- forwarded routes ---------------------------------------------------------------------


def test_messages_is_forwarded_with_the_gateway_bearer_and_no_inbound_credentials(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    status, _, body = relay.post_json(
        "/v1/messages?beta=true",
        {"model": "anthropic/claude-sonnet-4-5", "max_tokens": 16, "messages": []},
        headers={
            "Authorization": "Bearer worker-dummy",
            "X-Api-Key": "worker-key",
            "Proxy-Authorization": "Basic d29ya2Vy",
            "Cookie": "session=abc",
            "anthropic-version": "2023-06-01",
            "x-omp-install-id": "worker-install",
        },
    )
    assert (status, json.loads(body)) == (200, {"ok": True})
    (seen,) = upstream.forwarded()
    assert (seen.method, seen.path) == ("POST", "/v1/messages?beta=true")
    assert seen.headers["authorization"] == [f"Bearer {TOKEN}"]
    for stripped in ("x-api-key", "proxy-authorization", "cookie"):
        assert stripped not in seen.headers
    assert seen.header("anthropic-version") == "2023-06-01"
    assert seen.header("x-omp-install-id") == "worker-install"
    assert seen.header("host") == upstream.url.removeprefix("http://")


def test_pi_stream_is_forwarded_with_the_gateway_bearer(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    document = {"modelId": "openai-codex/gpt-5.2", "context": {"messages": []}, "stream": True}
    status, _, _ = relay.post_json(
        "/v1/pi/stream", document, headers={"Authorization": "Bearer enginery-relay"}
    )
    assert status == 200
    (seen,) = upstream.forwarded()
    assert seen.path == "/v1/pi/stream"
    assert seen.headers["authorization"] == [f"Bearer {TOKEN}"]
    assert seen.json() == document


def test_an_allowed_request_is_forwarded_byte_for_byte(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    raw = b'{ "model" : "anthropic/claude-opus-4-5",\n "messages": [ ] , "x": 1.50 }'
    status, _, _ = relay.request(
        "POST", "/v1/messages", body=raw, headers={"Content-Type": "application/json"}
    )
    assert status == 200
    assert upstream.forwarded()[0].body == raw


def test_a_bare_claude_id_on_messages_is_canonicalized_to_anthropic(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    status, _, _ = relay.post_json("/v1/messages", CLAUDE_BODY)
    assert status == 200
    (seen,) = upstream.forwarded()
    assert seen.json() == {**CLAUDE_BODY, "model": "anthropic/claude-sonnet-4-5"}


@pytest.mark.parametrize(
    "document",
    [
        {"modelId": "anthropic/claude-sonnet-4-5"},
        {"modelId": "", "model": "anthropic/claude-sonnet-4-5"},
        {"model": "openai-codex/gpt-5.2"},
        {"model": {"id": "anthropic/claude-sonnet-4-5"}},
        {"modelId": "anthropic/claude-sonnet-4-5", "model": "google-antigravity/gemini"},
        {"modelId": 5, "model": "anthropic/claude-sonnet-4-5"},
    ],
)
def test_pi_stream_resolves_the_model_the_way_the_gateway_does(
    relay: RelayProcess, upstream: RecordingUpstream, document: dict[str, object]
) -> None:
    status, _, _ = relay.post_json("/v1/pi/stream", {**document, "context": {"messages": []}})
    assert status == 200
    assert len(upstream.forwarded()) == 1


# --- refused routes -------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/credentials/check", "/v1/usage"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_credential_and_usage_routes_are_denied_for_every_method(
    relay: RelayProcess, upstream: RecordingUpstream, path: str, method: str
) -> None:
    status, _, body = relay.request(method, path, body=b"{}" if method != "GET" else None)
    assert status == 403
    assert json.loads(body)["error"]["type"] == "permission_error"
    assert upstream.forwarded() == []


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/v1/messages"),
        ("GET", "/v1/pi/stream"),
        ("POST", "/v1/models"),
        ("DELETE", "/v1/messages"),
        ("PUT", "/v1/models"),
        ("PATCH", "/v1/pi/stream"),
        ("OPTIONS", "/v1/messages"),
        ("HEAD", "/v1/models"),
        ("POST", "/healthz"),
    ],
)
def test_a_known_route_with_the_wrong_method_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream, method: str, path: str
) -> None:
    status, headers, _ = relay.request(method, path, headers={"Content-Length": "0"})
    assert status == 405
    assert "Allow" in headers
    assert upstream.forwarded() == []


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/v1",
        "/v1/chat/completions",
        "/v1/messages/",
        "/v1/messages/count_tokens",
        "/v1/models/anthropic%2Fx",
        "/v1/embeddings",
        "/v1/images/generations",
        "/v1/videos/abc",
        "/v1/pi/stream/extra",
        "/V1/MESSAGES",
        "/v1/../v1/messages",
        "/healthz/x",
        "/v1/%6Dessages",
    ],
)
def test_every_other_path_is_never_forwarded(
    relay: RelayProcess, upstream: RecordingUpstream, path: str
) -> None:
    for method in ("GET", "POST"):
        status, _, _ = relay.request(method, path, body=b"{}" if method == "POST" else None)
        assert status in (400, 404)
    assert upstream.forwarded() == []


def test_an_absolute_form_request_target_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as connection:
        connection.sendall(
            b"POST http://evil.example/v1/messages HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        reply = connection.recv(4096)
    assert reply.startswith(b"HTTP/1.1 400")
    assert upstream.forwarded() == []


def test_healthz_is_answered_locally_and_never_forwarded(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    status, _, body = relay.request("GET", "/healthz")
    assert (status, json.loads(body)["status"]) == (200, "ok")
    assert upstream.forwarded() == []


# --- model policy ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "google-antigravity/gemini-3-pro",
        "google-antigravity/claude-sonnet-4-5",
        "openai/gpt-5",
        "openrouter/anthropic/claude-3",
        "gpt-5.2",
        "gemini-3-pro",
        "claude",
        "Claude-sonnet-4-5",
        "Anthropic/claude-sonnet-4-5",
        "ANTHROPIC/claude-sonnet-4-5",
        "OpenAI-Codex/gpt-5",
        " anthropic/claude-sonnet-4-5",
        "anthropic/claude-sonnet-4-5 ",
        "anthropic/claude-sonnet-4-5\n",
        "anthropic/claude\x00sonnet",
        "anthropic/claude\u200bsonnet",
        "anthropic/cl\u0430ude-sonnet",
        "\uff41nthropic/claude-sonnet-4-5",
        "anthropic/",
        "openai-codex/",
        "anthropic",
        "",
        "@smol",
        "anthropic\\claude",
        "x/anthropic/claude-sonnet-4-5",
    ],
)
def test_a_model_outside_the_allowlist_never_reaches_the_gateway(
    relay: RelayProcess, upstream: RecordingUpstream, model: str
) -> None:
    for route, document in (
        ("/v1/messages", {"model": model, "messages": []}),
        ("/v1/pi/stream", {"modelId": model, "context": {"messages": []}}),
        ("/v1/pi/stream", {"model": model, "context": {"messages": []}}),
        ("/v1/pi/stream", {"model": {"id": model}, "context": {"messages": []}}),
    ):
        status, _, body = relay.post_json(route, document)
        assert status == 403, (route, document)
        assert json.loads(body)["error"]["type"] == "permission_error"
    assert upstream.forwarded() == []


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"messages": []},
        {"model": None},
        {"model": 5},
        {"model": ["anthropic/claude"]},
        {"model": {"provider": "anthropic"}},
        {"model": {"id": 7}},
    ],
)
def test_a_messages_body_without_a_usable_top_level_model_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream, document: dict[str, object]
) -> None:
    status, _, _ = relay.post_json("/v1/messages", document)
    assert status == 403
    assert upstream.forwarded() == []


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"context": {"messages": []}},
        {"modelId": None, "model": None},
        {"modelId": 5},
        {"model": ["anthropic/claude"]},
        {"model": {}},
        {"model": {"provider": "anthropic", "id": ""}},
        {"modelId": "google-antigravity/x", "model": "anthropic/claude"},
        {"modelId": "gpt-5", "model": "anthropic/claude-x"},
    ],
)
def test_a_pi_stream_body_without_a_permitted_gateway_model_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream, document: dict[str, object]
) -> None:
    status, _, _ = relay.post_json("/v1/pi/stream", document)
    assert status == 403
    assert upstream.forwarded() == []


def test_a_bare_claude_id_is_refused_on_pi_stream(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    status, _, _ = relay.post_json(
        "/v1/pi/stream", {"modelId": "claude-sonnet-4-5", "context": {"messages": []}}
    )
    assert status == 403
    assert upstream.forwarded() == []


# --- request validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'"anthropic/claude"',
        b"null",
        b'{"model": "anthropic/claude", "model": "openai/gpt"}',
        b'{"model": "anthropic/claude"',
        b"\xff\xfe{\x00}\x00",
        '{"model": "anthropic/claude"}'.encode("utf-16"),
        b"\xef\xbb\xbf" + b'{"model": "anthropic/claude"}',
        b"[" * 100000,
    ],
)
def test_an_invalid_json_body_is_rejected(
    relay: RelayProcess, upstream: RecordingUpstream, raw: bytes
) -> None:
    status, _, _ = relay.request("POST", "/v1/messages", body=raw)
    assert status == 400
    assert upstream.forwarded() == []


def test_an_oversized_body_is_rejected_before_it_is_read(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as connection:
        connection.sendall(
            b"POST /v1/messages HTTP/1.1\r\nHost: x\r\n"
            + f"Content-Length: {8 * 1024 * 1024 + 1}\r\n\r\n".encode()
        )
        reply = connection.recv(4096)
    assert reply.startswith(b"HTTP/1.1 413")
    assert upstream.forwarded() == []


def test_a_body_at_the_limit_is_accepted(relay: RelayProcess, upstream: RecordingUpstream) -> None:
    filler = "x" * (8 * 1024 * 1024 - 200)
    document = {"model": "anthropic/claude-sonnet-4-5", "messages": [], "filler": filler}
    status, _, _ = relay.post_json("/v1/messages", document)
    assert status == 200
    assert len(upstream.forwarded()) == 1


def test_a_post_without_content_length_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as connection:
        connection.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\n\r\n")
        reply = connection.recv(4096)
    assert reply.startswith(b"HTTP/1.1 411")
    assert upstream.forwarded() == []


def test_a_chunked_request_body_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    payload = b'{"model":"anthropic/claude-sonnet-4-5"}'
    with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as connection:
        connection.sendall(
            b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(payload):x}\r\n".encode()
            + payload
            + b"\r\n0\r\n\r\n"
        )
        reply = connection.recv(4096)
    assert reply.startswith(b"HTTP/1.1 411")
    assert upstream.forwarded() == []


@pytest.mark.parametrize("length", ["-1", "abc", "1e3", "+5", "5, 5"])
def test_a_malformed_content_length_is_refused(
    relay: RelayProcess, upstream: RecordingUpstream, length: str
) -> None:
    with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as connection:
        head = f"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n"
        connection.sendall(head.encode() + b"{}")
        reply = connection.recv(4096)
    assert reply.startswith(b"HTTP/1.1 400")
    assert upstream.forwarded() == []


def test_a_get_with_a_body_is_refused(relay: RelayProcess, upstream: RecordingUpstream) -> None:
    status, _, _ = relay.request("GET", "/v1/models", body=b"{}")
    assert status == 400
    assert upstream.forwarded() == []


# --- /v1/models -----------------------------------------------------------------------------


def _model_rows() -> list[dict[str, object]]:
    return [
        {"id": "anthropic/claude-sonnet-4-5", "object": "model", "owned_by": "anthropic"},
        {"id": "google-antigravity/gemini-3-pro", "object": "model", "owned_by": "x"},
        {"id": "openai-codex/gpt-5.2", "object": "model", "context_length": 400000},
        {"id": "openai/gpt-5", "object": "model"},
        {"id": "openrouter/anthropic/claude-3", "object": "model"},
        {"id": "claude-sonnet-4-5", "object": "model"},
        {"id": "Anthropic/x", "object": "model"},
        {"object": "model"},
    ]


def test_models_are_filtered_to_the_permitted_providers(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    upstream.script.models_document = {"object": "list", "data": _model_rows()}
    status, headers, body = relay.request(
        "GET", "/v1/models?limit=5", headers={"Authorization": "Bearer worker-dummy"}
    )
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    document = json.loads(body)
    assert document["object"] == "list"
    assert [row["id"] for row in document["data"]] == [
        "anthropic/claude-sonnet-4-5",
        "openai-codex/gpt-5.2",
    ]
    assert document["data"][1]["context_length"] == 400000
    (seen,) = upstream.forwarded()
    assert seen.path == "/v1/models?limit=5"
    assert seen.headers["authorization"] == [f"Bearer {TOKEN}"]
    assert seen.header("accept-encoding") == "identity"


@pytest.mark.parametrize(
    "document",
    [[], {"object": "list"}, {"data": "x"}, {"data": [1]}, {"data": [{"id": "anthropic/x"}, None]}],
)
def test_an_unexpected_model_list_is_never_passed_on(
    relay: RelayProcess, upstream: RecordingUpstream, document: object
) -> None:
    upstream.script.models_document = document
    status, _, body = relay.request("GET", "/v1/models")
    assert status == 502
    assert "anthropic" not in body.decode()


def test_an_upstream_error_status_is_passed_through_for_the_model_list(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    def deny(handler: BaseHTTPRequestHandler) -> bool:
        payload = b'{"error":"unauthorized"}'
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    upstream.script.handler = deny
    status, _, body = relay.request("GET", "/v1/models")
    assert (status, json.loads(body)) == (401, {"error": "unauthorized"})


# --- streaming ------------------------------------------------------------------------------


def test_an_sse_response_is_streamed_incrementally(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    release = threading.Event()
    released_after_first_event: list[bool] = []

    def stream(handler: BaseHTTPRequestHandler) -> bool:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()

        def chunk(data: bytes) -> None:
            handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            handler.wfile.flush()

        chunk(b"event: first\ndata: 1\n\n")
        released_after_first_event.append(release.wait(timeout=10))
        chunk(b"event: second\ndata: 2\n\n")
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
        return True

    upstream.script.handler = stream
    connection = relay.connection(timeout=20)
    try:
        connection.request(
            "POST",
            "/v1/pi/stream",
            body=json.dumps({"modelId": "anthropic/claude-sonnet-4-5", "context": {}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        first = b""
        while b"\n\n" not in first:
            piece = response.read1(1024)
            assert piece, "stream ended before the first event arrived"
            first += piece
        assert b"event: first" in first and b"second" not in first
        release.set()
        rest = response.read()
    finally:
        release.set()
        connection.close()
    assert released_after_first_event == [True]
    assert b"event: second" in rest


def test_an_upstream_that_closes_mid_stream_is_seen_as_a_truncated_stream(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    def broken(handler: BaseHTTPRequestHandler) -> bool:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        data = b"event: first\n\n"
        handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        handler.wfile.flush()
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)
        return True

    upstream.script.handler = broken
    connection = relay.connection(timeout=20)
    try:
        connection.request(
            "POST",
            "/v1/pi/stream",
            body=json.dumps({"modelId": "anthropic/claude-sonnet-4-5", "context": {}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        with pytest.raises(Exception, match=r"IncompleteRead|incomplete|Connection"):
            response.read()
    finally:
        connection.close()


def test_upstream_error_bodies_and_statuses_pass_through_on_the_post_routes(
    relay: RelayProcess, upstream: RecordingUpstream
) -> None:
    upstream.script.status = 429
    upstream.script.body = b'{"error":{"type":"rate_limit_error"}}'
    status, headers, body = relay.post_json("/v1/messages", CLAUDE_BODY)
    assert status == 429
    assert json.loads(body)["error"]["type"] == "rate_limit_error"
    assert headers["Content-Type"] == "application/json"


# --- the token ------------------------------------------------------------------------------


def test_the_token_is_read_again_on_every_request(
    relay: RelayProcess, upstream: RecordingUpstream, token_file: Path
) -> None:
    relay.post_json("/v1/messages", CLAUDE_BODY)
    write_token(token_file, "second-token-value")
    relay.post_json("/v1/messages", CLAUDE_BODY)
    bearers = [seen.headers["authorization"] for seen in upstream.forwarded()]
    assert bearers == [[f"Bearer {TOKEN}"], ["Bearer second-token-value"]]


@pytest.mark.parametrize("content", [b"", b"   \n", b"two words\n", b"\xff\xfe", b"a" * 5000])
def test_an_unusable_token_file_refuses_everything_and_forwards_nothing(
    relay: RelayProcess, upstream: RecordingUpstream, token_file: Path, content: bytes
) -> None:
    token_file.write_bytes(content)
    status, _, body = relay.post_json("/v1/messages", CLAUDE_BODY)
    assert status == 503
    assert "token" in json.loads(body)["error"]["message"]
    assert relay.request("GET", "/v1/models")[0] == 503
    health_status, _, health = relay.request("GET", "/healthz")
    assert health_status == 503
    assert json.loads(health)["status"] == "unavailable"
    assert upstream.forwarded() == []


def test_a_missing_token_file_refuses_everything(
    relay: RelayProcess, upstream: RecordingUpstream, token_file: Path
) -> None:
    token_file.unlink()
    assert relay.post_json("/v1/messages", CLAUDE_BODY)[0] == 503
    assert upstream.forwarded() == []


def test_neither_the_token_nor_a_body_is_ever_logged(
    upstream: RecordingUpstream, token_file: Path
) -> None:
    secret_body = "super-secret-prompt-text"
    with running_relay(upstream.url, token_file) as relay:
        relay.post_json(
            "/v1/messages?trace=1",
            {**CLAUDE_BODY, "messages": [{"role": "user", "content": secret_body}]},
            headers={"Authorization": "Bearer worker-dummy", "X-Api-Key": "worker-key"},
        )
        relay.post_json("/v1/messages", {"model": "openai/gpt-5", "x": secret_body})
        relay.request("GET", "/v1/usage")
    log = relay.stderr_text
    assert "/v1/messages" in log and "model-denied" in log and "denied" in log
    for forbidden in (TOKEN, secret_body, "worker-dummy", "worker-key", "trace=1"):
        assert forbidden not in log


# --- failures -------------------------------------------------------------------------------


def test_an_unreachable_upstream_is_a_bad_gateway(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    token = write_token(tmp_path / "token")
    with running_relay(f"http://127.0.0.1:{port}", token) as relay:
        status, _, body = relay.post_json("/v1/messages", CLAUDE_BODY)
        assert status == 502
        assert TOKEN not in body.decode()
        assert relay.request("GET", "/v1/models")[0] == 502


@pytest.mark.parametrize(
    "upstream_url",
    ["", "ftp://x:1", "http://", "http://x/path", "http://x?q=1", "http://user:pw@x:1", "x:1"],
)
def test_a_malformed_upstream_stops_the_relay_at_startup(tmp_path: Path, upstream_url: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(RELAY_SCRIPT)],
        env={"RELAY_UPSTREAM": upstream_url, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 2
    assert "RELAY_UPSTREAM" in completed.stderr


# --- bounded load: request cap, in-flight limit, connection limit -----------------------------


def _hold_upstream(
    gate: threading.Event, entered: threading.Semaphore
) -> Callable[[BaseHTTPRequestHandler], bool]:
    def handler(request: BaseHTTPRequestHandler) -> bool:
        entered.release()
        gate.wait(20)
        payload = b'{"ok":true}'
        request.send_response(200)
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)
        return True

    return handler


def test_the_request_cap_is_eight_mib_and_the_limits_are_the_documented_ones() -> None:
    source = RELAY_SCRIPT.read_text("utf-8")

    assert "MAX_REQUEST_BYTES = 8 * 1024 * 1024" in source
    assert "DEFAULT_MAX_IN_FLIGHT = 4" in source
    assert "DEFAULT_MAX_CONNECTIONS = 32" in source


def test_requests_beyond_the_in_flight_limit_get_503_and_never_reach_the_gateway(
    upstream: RecordingUpstream, token_file: Path
) -> None:
    gate, entered = threading.Event(), threading.Semaphore(0)
    upstream.script.handler = _hold_upstream(gate, entered)
    held: list[int] = []
    with running_relay(upstream.url, token_file, {"RELAY_MAX_IN_FLIGHT": "2"}) as relay:

        def call() -> None:
            held.append(relay.post_json("/v1/messages", CLAUDE_BODY)[0])

        workers = [threading.Thread(target=call) for _ in range(2)]
        for worker in workers:
            worker.start()
        assert entered.acquire(timeout=15) and entered.acquire(timeout=15)
        try:
            status, headers, body = relay.post_json("/v1/messages", CLAUDE_BODY)
            assert status == 503
            assert headers.get("Retry-After") == "2"
            assert b"concurrency limit" in body
            assert len(upstream.forwarded()) == 2  # the third request never reached the gateway
            # The relay still answers its own health check while it is saturated.
            assert relay.request("GET", "/healthz")[0] == 200
        finally:
            gate.set()
            for worker in workers:
                worker.join(timeout=20)
        assert held == [200, 200]
        # The slots were released: the next request is served again.
        assert relay.post_json("/v1/messages", CLAUDE_BODY)[0] == 200


def test_a_refused_in_flight_request_is_not_counted_and_models_obey_the_same_limit(
    upstream: RecordingUpstream, token_file: Path
) -> None:
    gate, entered = threading.Event(), threading.Semaphore(0)
    upstream.script.handler = _hold_upstream(gate, entered)
    with running_relay(upstream.url, token_file, {"RELAY_MAX_IN_FLIGHT": "1"}) as relay:
        worker = threading.Thread(target=lambda: relay.post_json("/v1/messages", CLAUDE_BODY))
        worker.start()
        assert entered.acquire(timeout=15)
        try:
            assert relay.request("GET", "/v1/models")[0] == 503
            assert relay.post_json("/v1/pi/stream", {"modelId": "anthropic/x"})[0] == 503
        finally:
            gate.set()
            worker.join(timeout=20)
        upstream.script.handler = None
        assert relay.request("GET", "/v1/models")[0] == 200


def test_connections_beyond_the_connection_limit_are_answered_503_without_a_thread(
    upstream: RecordingUpstream, token_file: Path
) -> None:
    env = {"RELAY_MAX_CONNECTIONS": "2", "RELAY_MAX_IN_FLIGHT": "2"}
    with running_relay(upstream.url, token_file, env) as relay:
        idle = [socket.create_connection(("127.0.0.1", relay.port), timeout=10) for _ in range(2)]
        try:
            # Give the relay time to accept both and start their handler threads.
            deadline = time.monotonic() + 10
            reply = b""
            while time.monotonic() < deadline and not reply.startswith(b"HTTP/1.1 503"):
                with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as extra:
                    extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
                    reply = extra.recv(4096)
                time.sleep(0.05)
            assert reply.startswith(b"HTTP/1.1 503")
            assert b"Retry-After: 2" in reply
            assert b"connection limit" in reply
        finally:
            for connection in idle:
                connection.close()
        # Once the idle connections are gone the relay serves again.
        deadline = time.monotonic() + 10
        status = 0
        while time.monotonic() < deadline and status != 200:
            status = relay.request("GET", "/healthz")[0]
            time.sleep(0.05)
        assert status == 200
        assert upstream.forwarded() == []


@pytest.mark.parametrize("name", ["RELAY_MAX_IN_FLIGHT", "RELAY_MAX_CONNECTIONS"])
@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_a_malformed_limit_stops_the_relay_at_startup(name: str, value: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(RELAY_SCRIPT)],
        env={"RELAY_UPSTREAM": "http://127.0.0.1:9", "PATH": "/usr/bin:/bin", name: value},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 2
    assert name in completed.stderr
