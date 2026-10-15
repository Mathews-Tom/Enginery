"""A loopback model server that scripts one turn of tool calls for a real harness binary.

The server speaks just enough of the two wire formats the harness adapters can be pointed at
(the Anthropic Messages API for Claude Code and the pi-native stream protocol for omp) to make
the harness run its own permission machinery: the first model turn requests the scripted tool
calls, the next turn (which carries the tool results) ends the conversation. It never reaches
any other host and holds no credential. It answers on 127.0.0.1 unless a test that runs the
harness inside a throw-away container asks it to bind every interface (``bind_host``), so the
container can reach it through the engine's host gateway.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Self

Flavor = Literal["anthropic", "pi-native"]
STUB_TOKEN = "stub-not-a-credential"
"""The placeholder bearer value harness processes are launched with."""


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One tool call the scripted model turn asks the harness to make."""

    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """What the stub observed for one request (never the credential itself)."""

    method: str
    path: str
    authorization_scheme: str | None
    body: object


@dataclass(slots=True)
class LoopbackModelServer:
    """Serve scripted tool-calling turns, then a final text turn, on a loopback port."""

    flavor: Flavor
    turns: Sequence[Sequence[ToolRequest]]
    """One entry per scripted model turn; the turn after the last one answers ``final_text``."""
    final_text: str = "done"
    model: str = "claude-opus-5-5"
    bind_host: str = "127.0.0.1"
    requests: list[RecordedRequest] = field(default_factory=list, init=False)
    _server: ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def base_url(self) -> str:
        assert self._server is not None, "server is not started"
        return self.url_for("127.0.0.1")

    def url_for(self, host: str) -> str:
        """The base URL a client that reaches this server as ``host`` should use."""
        assert self._server is not None, "server is not started"
        return f"http://{host}:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                del args

            def do_GET(self) -> None:
                outer._record(self, None)
                if self.path.startswith("/v1/models"):
                    outer._reply_json(self, {"object": "list", "data": []})
                else:
                    outer._reply_json(self, {"error": {"type": "not_found", "message": "no"}}, 404)

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body: object = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = None
                outer._record(self, body)
                outer._answer(self, body)

        server = ThreadingHTTPServer((self.bind_host, 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _record(self, handler: BaseHTTPRequestHandler, body: object) -> None:
        authorization = handler.headers.get("authorization")
        scheme = authorization.split(" ", 1)[0] if authorization else None
        with self._lock:
            self.requests.append(
                RecordedRequest(
                    method=handler.command,
                    path=handler.path,
                    authorization_scheme=scheme,
                    body=body,
                )
            )

    def _reply_json(
        self, handler: BaseHTTPRequestHandler, payload: object, status: int = 200
    ) -> None:
        data = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _reply_sse(self, handler: BaseHTTPRequestHandler, frames: list[tuple[str, object]]) -> None:
        text = "".join(
            f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in frames
        )
        data = text.encode()
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _answer(self, handler: BaseHTTPRequestHandler, body: object) -> None:
        if self.flavor == "anthropic" and handler.path.split("?", 1)[0] == "/v1/messages":
            self._answer_anthropic(handler, body)
        elif self.flavor == "pi-native" and handler.path == "/v1/pi/stream":
            self._answer_pi_native(handler, body)
        else:
            self._reply_json(handler, {"error": {"type": "not_found", "message": "no"}}, 404)

    # -- Anthropic Messages API -------------------------------------------------------------

    def _answer_anthropic(self, handler: BaseHTTPRequestHandler, body: object) -> None:
        payload = body if isinstance(body, dict) else {}
        messages = payload.get("messages")
        round_index = _answered_rounds(messages, "toolu_")
        main_loop = bool(payload.get("tools"))
        blocks: list[dict[str, object]]
        if main_loop and round_index < len(self.turns):
            blocks = [
                {
                    "type": "tool_use",
                    "id": f"toolu_{round_index}_{index}",
                    "name": call.name,
                    "input": call.arguments,
                }
                for index, call in enumerate(self.turns[round_index])
            ]
            stop_reason = "tool_use"
        else:
            blocks = [{"type": "text", "text": self.final_text}]
            stop_reason = "end_turn"
        message: dict[str, object] = {
            "id": f"msg_stub_{round_index}",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        if payload.get("stream") is not True:
            self._reply_json(
                handler,
                {**message, "content": blocks, "stop_reason": stop_reason},
            )
            return
        frames: list[tuple[str, object]] = [
            ("message_start", {"type": "message_start", "message": message})
        ]
        for index, block in enumerate(blocks):
            start: dict[str, object]
            delta: dict[str, object]
            if block["type"] == "text":
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                start = {**block, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            frames += [
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": start},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": index}),
            ]
        frames += [
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        self._reply_sse(handler, frames)

    # -- pi-native stream -------------------------------------------------------------------

    def _answer_pi_native(self, handler: BaseHTTPRequestHandler, body: object) -> None:
        payload = body if isinstance(body, dict) else {}
        context = payload.get("context")
        messages = context.get("messages") if isinstance(context, dict) else None
        round_index = _answered_rounds(messages, "call_")
        usage = {
            "input": 1,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 2,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        }
        content: list[dict[str, object]]
        if round_index < len(self.turns):
            content = [
                {
                    "type": "toolCall",
                    "id": f"call_{round_index}_{index}",
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for index, call in enumerate(self.turns[round_index])
            ]
            stop_reason = "toolUse"
        else:
            content = [{"type": "text", "text": self.final_text}]
            stop_reason = "stop"
        model_id = payload.get("modelId")
        model_name = model_id.split("/", 1)[-1] if isinstance(model_id, str) else self.model
        message = {
            "role": "assistant",
            "content": content,
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": model_name,
            "usage": usage,
            "stopReason": stop_reason,
            "timestamp": 0,
        }
        empty = {**message, "content": [], "stopReason": "stop"}
        events: list[dict[str, object]] = [{"type": "start", "partial": empty}]
        for index, block in enumerate(content):
            if block["type"] == "text":
                events += [
                    {"type": "text_start", "contentIndex": index, "partial": message},
                    {
                        "type": "text_delta",
                        "contentIndex": index,
                        "delta": block["text"],
                        "partial": message,
                    },
                    {
                        "type": "text_end",
                        "contentIndex": index,
                        "content": block["text"],
                        "partial": message,
                    },
                ]
            else:
                events += [
                    {"type": "toolcall_start", "contentIndex": index, "partial": message},
                    {
                        "type": "toolcall_end",
                        "contentIndex": index,
                        "toolCall": block,
                        "partial": message,
                    },
                ]
        events.append(
            {
                "type": "done",
                "reason": "toolUse" if stop_reason == "toolUse" else "stop",
                "message": message,
            }
        )
        self._reply_sse(handler, [("message", event) for event in events])


def _answered_rounds(messages: object, id_prefix: str) -> int:
    """How many scripted turns the conversation has already received.

    Tool call ids are ``<prefix><round>_<index>``, so the next turn is one past the highest round
    seen. Counting assistant messages instead would break when a harness merges consecutive ones.
    """
    if not isinstance(messages, list):
        return 0
    rounds = -1
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"tool_use", "toolCall"}:
                identifier = block.get("id")
                if isinstance(identifier, str) and identifier.startswith(id_prefix):
                    rounds = max(rounds, int(identifier[len(id_prefix) :].split("_", 1)[0]))
    return rounds + 1


Predicate = Callable[[RecordedRequest], bool]


def model_requests(server: LoopbackModelServer, predicate: Predicate) -> list[RecordedRequest]:
    """The recorded requests matching ``predicate`` (a thin helper for assertions)."""
    return [request for request in server.requests if predicate(request)]
