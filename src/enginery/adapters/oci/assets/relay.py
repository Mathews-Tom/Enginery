#!/usr/bin/env python3
"""Operator-owned model-access relay (standard library only, Python 3.12).

A worker container has no route to the model gateway and holds no gateway credential. It talks
to this relay, which is the only process that holds the gateway bearer. The relay forwards
exactly three routes and nothing else:

    POST /v1/messages     Anthropic messages (Claude Code)
    POST /v1/pi/stream    pi-native stream (omp)
    GET  /v1/models       model list, filtered to the permitted providers

Every other method and path is refused locally and never reaches the gateway. The relay answers
``GET /healthz`` itself. For the two POST routes it reads the model the gateway would route on
(with the gateway's own precedence), refuses anything outside ``anthropic/`` and
``openai-codex/``, and rewrites a bare ``claude-*`` id on ``/v1/messages`` to
``anthropic/<id>`` because the gateway's other providers share bare ids. It strips every inbound
credential header and injects ``Authorization: Bearer <token>``, reading the token file again
on every request. Neither the token nor a request body is ever logged.

Configuration (environment): ``RELAY_UPSTREAM`` (``http://host:port``, required),
``RELAY_TOKEN_FILE`` (default ``/run/relay/gateway-token``), ``RELAY_LISTEN_HOST`` (default
``0.0.0.0``), ``RELAY_LISTEN_PORT`` (default ``8080``; ``0`` picks a free port and is announced
on stdout), ``RELAY_CONNECT_TIMEOUT`` (seconds, default 10), ``RELAY_READ_TIMEOUT`` (seconds,
default 900), ``RELAY_MAX_IN_FLIGHT`` (forwarded requests at once, default 4),
``RELAY_MAX_CONNECTIONS`` (open connections at once, default 32),
``RELAY_MAX_IN_FLIGHT_PER_SOURCE`` (default 2), ``RELAY_MAX_CONNECTIONS_PER_SOURCE`` (default 8)
and ``RELAY_BODY_DEADLINE`` (seconds allowed to receive one request body, default 60).

Requests are bounded: a body over 8 MiB is refused with 413, a body that is not fully received
within the body deadline is refused with 408 (a client that trickles bytes cannot keep a slot),
a forwarded request beyond the in-flight limit is refused with 503 and ``Retry-After`` before
its body is read, and connections beyond the connection limit are answered 503 and closed.
Every limit also applies per source: each attempt reaches the relay from its own network, so the
client address identifies the attempt, and one source can hold at most its per-source share of
the in-flight slots and of the connections. Fairness is only that: with more sources than
``RELAY_MAX_IN_FLIGHT / RELAY_MAX_IN_FLIGHT_PER_SOURCE`` the global limit is still shared first
come, first served, and a source's response stream holds its slot for as long as the upstream
keeps it open (at most ``RELAY_READ_TIMEOUT`` between chunks). The container restarts the relay
after a crash (or an out-of-memory kill); until it answers ``/healthz`` again, admission holds.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NoReturn

ALLOWED_MODEL_PREFIXES = ("anthropic/", "openai-codex/")
BARE_CLAUDE_PREFIX = "claude-"
# Request bounds. The relay buffers each POST body in full (it must read the model out of the JSON
# and may rewrite it), so its memory is bounded by (body cap x requests in flight), not by the
# number of connections.
#
# MAX_REQUEST_BYTES = 8 MiB. A full 1M-token text context is about 4 MiB of JSON (roughly four
# bytes a token), so 8 MiB keeps every real long-context request working, with room for large tool
# results and a few base64 screenshots. The previous 32 MiB let eight concurrent POSTs exceed the
# relay container's 256 MiB memory limit.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
# DEFAULT_MAX_IN_FLIGHT = 4 forwarded requests at once. Each holds the raw body, its decoded text
# and its parsed document (and a re-serialized copy when the model id is canonicalized): about
# 30-40 MiB for an 8 MiB body, so four fit the 256 MiB limit; a request beyond four gets 503 with
# Retry-After, which both harnesses retry with backoff. Responses stream and are not buffered.
DEFAULT_MAX_IN_FLIGHT = 4
# DEFAULT_MAX_CONNECTIONS = 32 sockets (handler threads) at once, half the container's 64-process
# limit, so a flood of slow connections cannot exhaust it; beyond that a connection is answered
# 503 and closed without a thread. ``/healthz`` is never counted against the in-flight limit.
DEFAULT_MAX_CONNECTIONS = 32
# Per-source shares (a source is the client address; every attempt has its own network and so its
# own address): one source may hold at most half of the in-flight slots and a quarter of the
# connections, so one worker's parallel or stalled requests never take everything.
DEFAULT_MAX_IN_FLIGHT_PER_SOURCE = 2
DEFAULT_MAX_CONNECTIONS_PER_SOURCE = 8
# DEFAULT_BODY_DEADLINE = 60 seconds in total to receive a request body (at most 8 MiB from a
# peer on a local bridge network), however slowly it arrives; the per-read socket timeout alone
# lets a client trickle one byte every 119 seconds and hold its slot for days.
DEFAULT_BODY_DEADLINE = 60.0
RETRY_AFTER_SECONDS = 2
SHED_SEND_TIMEOUT_SECONDS = 1.0
MAX_MODELS_BYTES = 16 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 1024 * 1024
MAX_TOKEN_FILE_BYTES = 4096
MAX_MODEL_ID_CHARS = 256
STREAM_CHUNK_BYTES = 64 * 1024
CLIENT_SOCKET_TIMEOUT_SECONDS = 120

DEFAULT_TOKEN_FILE = "/run/relay/gateway-token"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8080
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 900.0

ROUTE_MESSAGES = "/v1/messages"
ROUTE_PI_STREAM = "/v1/pi/stream"
ROUTE_MODELS = "/v1/models"
ROUTE_HEALTH = "/healthz"
FORWARDED_ROUTES = {
    ROUTE_MESSAGES: "POST",
    ROUTE_PI_STREAM: "POST",
    ROUTE_MODELS: "GET",
}
DENIED_ROUTES = frozenset({"/v1/credentials/check", "/v1/usage"})

# Hop-by-hop headers (RFC 9110 section 7.6.1) plus the ones the relay sets itself.
_DROPPED_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "expect",
        "host",
        "content-length",
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    }
)
_CREDENTIAL_HEADER_MARKERS = ("authorization", "api-key", "apikey", "auth-token", "cookie")


class RelayConfigError(Exception):
    """The relay was started with unusable configuration."""


@dataclass(frozen=True)
class RelayConfig:
    upstream_scheme: str
    upstream_host: str
    upstream_port: int
    token_file: str
    listen_host: str
    listen_port: int
    connect_timeout: float
    read_timeout: float
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_in_flight_per_source: int = DEFAULT_MAX_IN_FLIGHT_PER_SOURCE
    max_connections_per_source: int = DEFAULT_MAX_CONNECTIONS_PER_SOURCE
    body_deadline: float = DEFAULT_BODY_DEADLINE

    @property
    def upstream_netloc(self) -> str:
        default = 443 if self.upstream_scheme == "https" else 80
        host = f"[{self.upstream_host}]" if ":" in self.upstream_host else self.upstream_host
        return host if self.upstream_port == default else f"{host}:{self.upstream_port}"


def parse_config(environ: dict[str, str]) -> RelayConfig:
    upstream = environ.get("RELAY_UPSTREAM", "")
    parts = urllib.parse.urlsplit(upstream)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RelayConfigError("RELAY_UPSTREAM must be http(s)://host[:port]")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise RelayConfigError("RELAY_UPSTREAM must carry no path, query or credentials")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        listen_port = int(environ.get("RELAY_LISTEN_PORT", str(DEFAULT_LISTEN_PORT)))
        connect_timeout = float(environ.get("RELAY_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT))
        read_timeout = float(environ.get("RELAY_READ_TIMEOUT", DEFAULT_READ_TIMEOUT))
    except ValueError as error:
        raise RelayConfigError(f"relay configuration is malformed: {error}") from error
    try:
        body_deadline = float(environ.get("RELAY_BODY_DEADLINE", DEFAULT_BODY_DEADLINE))
    except ValueError as error:
        raise RelayConfigError(
            "RELAY_BODY_DEADLINE must be a positive number of seconds"
        ) from error
    if not 0 <= listen_port <= 65535 or connect_timeout <= 0 or read_timeout <= 0:
        raise RelayConfigError("relay port or timeouts are out of range")
    if not 0 < body_deadline < float("inf"):
        raise RelayConfigError("RELAY_BODY_DEADLINE must be a positive number of seconds")
    max_in_flight = _positive_int(environ, "RELAY_MAX_IN_FLIGHT", DEFAULT_MAX_IN_FLIGHT)
    max_connections = _positive_int(environ, "RELAY_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS)
    max_in_flight_per_source = _positive_int(
        environ, "RELAY_MAX_IN_FLIGHT_PER_SOURCE", DEFAULT_MAX_IN_FLIGHT_PER_SOURCE
    )
    max_connections_per_source = _positive_int(
        environ, "RELAY_MAX_CONNECTIONS_PER_SOURCE", DEFAULT_MAX_CONNECTIONS_PER_SOURCE
    )
    return RelayConfig(
        upstream_scheme=parts.scheme,
        upstream_host=parts.hostname,
        upstream_port=port,
        token_file=environ.get("RELAY_TOKEN_FILE", DEFAULT_TOKEN_FILE),
        listen_host=environ.get("RELAY_LISTEN_HOST", DEFAULT_LISTEN_HOST),
        listen_port=listen_port,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_in_flight=max_in_flight,
        max_connections=max_connections,
        max_in_flight_per_source=max_in_flight_per_source,
        max_connections_per_source=max_connections_per_source,
        body_deadline=body_deadline,
    )


def _positive_int(environ: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as error:
        raise RelayConfigError(f"{name} must be an integer") from error
    if value < 1:
        raise RelayConfigError(f"{name} must be at least 1")
    return value


# --- policy ---------------------------------------------------------------------------------


def _clean_text(value: object) -> str | None:
    """``value`` when it is a non-empty ASCII string without controls or edge whitespace."""
    if not isinstance(value, str) or not value or len(value) > MAX_MODEL_ID_CHARS:
        return None
    if not value.isascii() or value != value.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def requested_model(route: str, body: dict[str, Any]) -> object:
    """The model identifier the gateway would route ``body`` on.

    ``/v1/messages`` routes on the top-level ``model`` string. ``/v1/pi/stream`` takes a
    non-empty ``modelId`` string, else a non-empty ``model`` string, else a non-empty
    ``model.id`` string: the same precedence the gateway applies. ``None`` when there is none.
    """
    if route == ROUTE_MESSAGES:
        model = body.get("model")
        return model if isinstance(model, str) else None
    model_id = body.get("modelId")
    if isinstance(model_id, str) and model_id:
        return model_id
    model = body.get("model")
    if isinstance(model, str) and model:
        return model
    if isinstance(model, dict):
        nested = model.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def canonical_model(route: str, raw: object) -> str | None:
    """The permitted, canonical model id for ``raw``, or ``None`` when the relay must refuse it.

    Matching is exact on the raw string: no stripping, no case folding, ASCII only. Only
    ``/v1/messages`` accepts a bare ``claude-*`` id (rewritten to ``anthropic/<id>``); every
    other bare id is ambiguous across the gateway's providers and is refused.
    """
    text = _clean_text(raw)
    if text is None:
        return None
    for prefix in ALLOWED_MODEL_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            return text
    if route == ROUTE_MESSAGES and text.startswith(BARE_CLAUDE_PREFIX):
        return f"anthropic/{text}"
    return None


def filter_models(document: object) -> dict[str, Any] | None:
    """``document`` with ``data`` reduced to permitted-provider models; ``None`` if malformed."""
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        return None
    rows: list[Any] = []
    for row in document["data"]:
        if not isinstance(row, dict):
            return None
        model_id = row.get("id")
        if isinstance(model_id, str) and model_id.startswith(ALLOWED_MODEL_PREFIXES):
            rows.append(row)
    return {**document, "data": rows}


def forwardable_headers(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Inbound headers that may reach the gateway: no hop-by-hop and no credential of any kind."""
    kept: list[tuple[str, str]] = []
    for name, value in items:
        lowered = name.lower()
        if lowered in _DROPPED_HEADERS:
            continue
        if any(marker in lowered for marker in _CREDENTIAL_HEADER_MARKERS):
            continue
        kept.append((name, value))
    return kept


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_json_object(raw: bytes) -> dict[str, Any]:
    """Strictly decode a request body into a JSON object (UTF-8, no duplicate keys)."""
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ValueError("request body is not a valid JSON object") from error
    if not isinstance(document, dict):
        raise ValueError("request body is not a JSON object")
    return document


def read_token(path: str) -> str | None:
    """The gateway token, read afresh; ``None`` when the file is missing, empty or unusable."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TOKEN_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_TOKEN_FILE_BYTES:
        return None
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not token or any(ord(char) < 33 or ord(char) == 127 for char in token):
        return None
    return token


# --- server ---------------------------------------------------------------------------------


class RefusalError(Exception):
    def __init__(
        self,
        status: int,
        error_type: str,
        message: str,
        decision: str,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message
        self.decision = decision
        self.headers = headers


def client_source(client_address: Any) -> str:
    """The source a client is counted under: its IP address (one per attempt network)."""
    return str(client_address[0])


class SourceLimiter:
    """Counts held slots per source and refuses a source its ``limit + 1``-th at once."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._held: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, source: str) -> bool:
        with self._lock:
            held = self._held.get(source, 0)
            if held >= self._limit:
                return False
            self._held[source] = held + 1
            return True

    def release(self, source: str) -> None:
        with self._lock:
            held = self._held.get(source, 0) - 1
            if held > 0:
                self._held[source] = held
            else:
                self._held.pop(source, None)


class RelayServer(ThreadingHTTPServer):
    """The relay's HTTP server, with a bound on connections and on forwarded requests.

    ``in_flight`` admits at most ``max_in_flight`` forwarded requests at once (the rest get 503
    and ``Retry-After``), and ``source_in_flight`` at most ``max_in_flight_per_source`` of them
    per source. Connections beyond ``max_connections`` (or ``max_connections_per_source`` from one
    source) are answered 503 and closed on the accept thread, without spawning a handler thread,
    so slow or numerous clients cannot exhaust the container's process limit.

    ``source_key`` maps a client address to the source it is counted under (default: its IP
    address, which is one per attempt network). It must be a pure function of the address.
    """

    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self, config: RelayConfig, source_key: Callable[[Any], str] = client_source
    ) -> None:
        self.config = config
        self.source_key = source_key
        self.in_flight = threading.BoundedSemaphore(config.max_in_flight)
        self.source_in_flight = SourceLimiter(config.max_in_flight_per_source)
        self._connections = threading.BoundedSemaphore(config.max_connections)
        self._source_connections = SourceLimiter(config.max_connections_per_source)
        super().__init__((config.listen_host, config.listen_port), RelayHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        source = self.source_key(client_address)
        if not self._source_connections.acquire(source):
            self._shed(request, "this source is at its connection limit; retry shortly")
            self.shutdown_request(request)
            return
        if not self._connections.acquire(blocking=False):
            self._source_connections.release(source)
            self._shed(request, "the relay is at its connection limit; retry shortly")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_connection(source)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_connection(self.source_key(client_address))

    def _release_connection(self, source: str) -> None:
        self._connections.release()
        self._source_connections.release(source)

    @staticmethod
    def _shed(request: socket.socket, message: str) -> None:
        payload = json.dumps(
            {"error": {"type": "overloaded_error", "message": message}},
            separators=(",", ":"),
        ).encode("utf-8")
        head = (
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Retry-After: {RETRY_AFTER_SECONDS}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        try:
            request.settimeout(SHED_SEND_TIMEOUT_SECONDS)
            request.sendall(head + payload)
        except OSError:
            pass
        sys.stderr.write("relay - connection-limit 503\n")
        sys.stderr.flush()


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "enginery-relay"
    sys_version = ""
    timeout = CLIENT_SOCKET_TIMEOUT_SECONDS
    server: RelayServer

    # -- logging: method, path (no query), decision, status; never a header or a body ----------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _log(self, decision: str, status: int) -> None:
        path = self.path.partition("?")[0]
        safe = "".join(char if 32 < ord(char) < 127 else "?" for char in path)[:128]
        sys.stderr.write(f"relay {self.command} {safe} {decision} {status}\n")
        sys.stderr.flush()

    # -- verbs ---------------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def do_TRACE(self) -> None:
        self._handle()

    def do_CONNECT(self) -> None:
        self._handle()

    # -- responses -----------------------------------------------------------------------------

    def _send_json(
        self,
        status: int,
        document: object,
        decision: str,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self._log(decision, status)

    def _refuse(self, refusal: RefusalError, headers: tuple[tuple[str, str], ...] = ()) -> None:
        document = {"error": {"type": refusal.error_type, "message": refusal.message}}
        self._send_json(refusal.status, document, refusal.decision, headers)

    # -- dispatch ------------------------------------------------------------------------------

    def _handle(self) -> None:
        self.close_connection = True
        target = self.path
        if not target.startswith("/") or target.startswith("//"):
            self._refuse(
                RefusalError(400, "invalid_request_error", "bad request target", "bad-target")
            )
            return
        route, _, query = target.partition("?")
        try:
            if route == ROUTE_HEALTH:
                self._health()
            elif route in DENIED_ROUTES:
                raise RefusalError(
                    403, "permission_error", "route is denied by the relay", "denied"
                )
            elif route in FORWARDED_ROUTES:
                self._forward_route(route, query)
            else:
                raise RefusalError(404, "not_found_error", "no such route", "unknown-route")
        except RefusalError as refusal:
            allow: tuple[tuple[str, str], ...] = ()
            if refusal.status == 405:
                allowed = "GET" if route == ROUTE_HEALTH else FORWARDED_ROUTES[route]
                allow = (("Allow", allowed),)
            self._refuse(refusal, (*refusal.headers, *allow))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:  # last resort: never leak internals, never hang the client
            self._send_json(
                500, {"error": {"type": "api_error", "message": "relay error"}}, "internal-error"
            )

    def _health(self) -> None:
        if self.command != "GET":
            raise RefusalError(405, "invalid_request_error", "method not allowed", "method")
        if read_token(self.server.config.token_file) is None:
            self._send_json(
                503,
                {
                    "status": "unavailable",
                    "reason": "the gateway token file is missing, unreadable or empty",
                },
                "health",
            )
            return
        self._send_json(200, {"status": "ok", "service": "enginery-relay"}, "health")

    def _forward_route(self, route: str, query: str) -> None:
        if self.command != FORWARDED_ROUTES[route]:
            raise RefusalError(405, "invalid_request_error", "method not allowed", "method")
        if "transfer-encoding" in {name.lower() for name in self.headers}:
            raise RefusalError(411, "invalid_request_error", "content-length is required", "length")
        # Admission comes before the body is read: a request beyond the limit is refused without
        # buffering anything, so the bound on memory is (body cap x in-flight limit).
        # A source is held to its own share first, so one attempt's parallel or stalled requests
        # leave the rest of the global limit to the other attempts.
        source = self.server.source_key(self.client_address)
        if not self.server.source_in_flight.acquire(source):
            raise RefusalError(
                503,
                "overloaded_error",
                "this source is at its concurrency limit; retry shortly",
                "busy-source",
                headers=(("Retry-After", str(RETRY_AFTER_SECONDS)),),
            )
        try:
            if not self.server.in_flight.acquire(blocking=False):
                raise RefusalError(
                    503,
                    "overloaded_error",
                    "the relay is at its concurrency limit; retry shortly",
                    "busy",
                    headers=(("Retry-After", str(RETRY_AFTER_SECONDS)),),
                )
            try:
                self._forward_admitted(route, query)
            finally:
                self.server.in_flight.release()
        finally:
            self.server.source_in_flight.release(source)

    def _forward_admitted(self, route: str, query: str) -> None:
        body = self._read_body(route)
        decision = "forward"
        if route != ROUTE_MODELS:
            assert body is not None
            try:
                document = parse_json_object(body)
            except ValueError as error:
                raise RefusalError(400, "invalid_request_error", str(error), "bad-json") from error
            raw_model = requested_model(route, document)
            model = canonical_model(route, raw_model)
            if model is None:
                raise RefusalError(
                    403, "permission_error", "model is not permitted by the relay", "model-denied"
                )
            if route == ROUTE_MESSAGES and model != raw_model:
                document["model"] = model
                body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                decision = "forward-canonicalized"
        token = read_token(self.server.config.token_file)
        if token is None:
            raise RefusalError(
                503, "api_error", "the relay has no usable gateway token", "token-unavailable"
            )
        self._proxy(route, query, body, token, decision)

    def _read_body(self, route: str) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if route == ROUTE_MODELS:
            if raw_length not in (None, "0"):
                raise RefusalError(400, "invalid_request_error", "unexpected request body", "body")
            return None
        if raw_length is None:
            raise RefusalError(411, "invalid_request_error", "content-length is required", "length")
        if not raw_length.isascii() or not raw_length.isdigit():
            raise RefusalError(400, "invalid_request_error", "content-length is invalid", "length")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise RefusalError(
                413, "invalid_request_error", "request body is too large", "too-large"
            )
        body = self._receive(length)
        if len(body) != length:
            raise RefusalError(
                400, "invalid_request_error", "request body is truncated", "truncated"
            )
        return body

    def _receive(self, length: int) -> bytes:
        """Read up to ``length`` body bytes within the total body deadline.

        Each read waits at most for what is left of the deadline, so a client that sends a byte
        at a time still runs out of time; a body that is not complete in time is a 408. A short
        result means the client closed early.
        """
        deadline = time.monotonic() + self.server.config.body_deadline
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(min(left, CLIENT_SOCKET_TIMEOUT_SECONDS))
                chunk = self.rfile.read1(min(remaining, STREAM_CHUNK_BYTES))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError as error:
            raise RefusalError(
                408, "invalid_request_error", "the request body was not received in time", "slow"
            ) from error
        finally:
            self.connection.settimeout(CLIENT_SOCKET_TIMEOUT_SECONDS)
        return b"".join(chunks)

    # -- forwarding ----------------------------------------------------------------------------

    def _upstream_connection(self) -> http.client.HTTPConnection:
        config = self.server.config
        connection_type = (
            http.client.HTTPSConnection
            if config.upstream_scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            config.upstream_host, config.upstream_port, timeout=config.connect_timeout
        )
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(config.read_timeout)
        return connection

    def _proxy(self, route: str, query: str, body: bytes | None, token: str, decision: str) -> None:
        config = self.server.config
        headers = forwardable_headers(list(self.headers.items()))
        outbound: dict[str, str] = {}
        for name, value in headers:
            outbound[name] = value
        if route == ROUTE_MODELS:
            for name in [name for name in outbound if name.lower() == "accept-encoding"]:
                del outbound[name]
            outbound["Accept-Encoding"] = "identity"
        outbound["Host"] = config.upstream_netloc
        outbound["Authorization"] = f"Bearer {token}"
        outbound["Connection"] = "close"
        if body is not None:
            outbound["Content-Length"] = str(len(body))
        target = route + (f"?{query}" if query else "")
        connection: http.client.HTTPConnection | None = None
        try:
            try:
                connection = self._upstream_connection()
                connection.request(self.command, target, body=body, headers=outbound)
                response = connection.getresponse()
            except (ValueError, http.client.InvalidURL) as error:
                raise RefusalError(
                    400, "invalid_request_error", "request is malformed", "bad-request"
                ) from error
            except (OSError, http.client.HTTPException) as error:
                raise RefusalError(
                    502, "api_error", "the upstream gateway is unreachable", "upstream-error"
                ) from error
            if route == ROUTE_MODELS:
                self._respond_models(response, decision)
            else:
                self._respond_stream(response, decision)
        finally:
            if connection is not None:
                connection.close()

    def _response_headers(self, response: http.client.HTTPResponse) -> list[tuple[str, str]]:
        return [
            (name, value)
            for name, value in response.getheaders()
            if name.lower() not in _DROPPED_HEADERS and name.lower() not in ("server", "date")
        ]

    def _respond_models(self, response: http.client.HTTPResponse, decision: str) -> None:
        try:
            raw = response.read(MAX_MODELS_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise RefusalError(
                502, "api_error", "the upstream gateway failed", "upstream-error"
            ) from error
        if len(raw) > MAX_MODELS_BYTES:
            raise RefusalError(
                502, "api_error", "the upstream model list is too large", "upstream-error"
            )
        status = response.status
        if status == 200:
            if response.getheader("Content-Encoding") not in (None, "", "identity"):
                raise RefusalError(
                    502, "api_error", "unexpected upstream encoding", "upstream-error"
                )
            try:
                filtered = filter_models(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, ValueError, RecursionError):
                filtered = None
            if filtered is None:
                raise RefusalError(
                    502, "api_error", "unexpected upstream model list", "upstream-error"
                )
            payload = json.dumps(filtered, separators=(",", ":")).encode("utf-8")
            kept = [("Content-Type", "application/json")]
        else:
            payload = raw[:MAX_ERROR_BODY_BYTES]
            kept = [
                (name, value)
                for name, value in self._response_headers(response)
                if name.lower() == "content-type"
            ]
        self.close_connection = True
        self.send_response(status)
        for name, value in kept:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self._log(decision, status)

    def _respond_stream(self, response: http.client.HTTPResponse, decision: str) -> None:
        status = response.status
        length = response.getheader("Content-Length")
        bodyless = status in (204, 304) or 100 <= status < 200
        chunked = length is None and not bodyless
        self.close_connection = True
        self.send_response(status)
        for name, value in self._response_headers(response):
            self.send_header(name, value)
        if length is not None and length.isdigit():
            self.send_header("Content-Length", length)
        elif chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        self._log(decision, status)
        if bodyless:
            return
        try:
            while True:
                chunk = response.read1(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # Headers are already sent: end the connection without a terminating chunk so the
            # client sees a truncated stream, never a clean end.
            return
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()


def serve(config: RelayConfig) -> NoReturn:
    server = RelayServer(config)
    address = server.server_address
    sys.stdout.write(f"relay listening {address[0]!s}:{address[1]}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()
    raise SystemExit(0)


def main() -> int:
    try:
        config = parse_config(dict(os.environ))
    except RelayConfigError as error:
        sys.stderr.write(f"relay: {error}\n")
        return 2
    serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
