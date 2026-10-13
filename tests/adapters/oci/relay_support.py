"""Test support for the model-access relay: a recording loopback upstream and a relay process.

The relay under test is the real packaged ``relay.py`` run as a subprocess. The upstream is a
loopback HTTP server that records every request it receives, so a test can assert both what was
forwarded and, as importantly, that a refused request never reached it.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

RELAY_SCRIPT = Path(str(resources.files("enginery.adapters.oci").joinpath("assets/relay.py")))
TOKEN = "gw-test-token-0123456789abcdef"


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, list[str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class UpstreamScript:
    """How the recording upstream answers each request."""

    status: int = 200
    body: bytes = b'{"ok":true}'
    content_type: str = "application/json"
    handler: Callable[[BaseHTTPRequestHandler], bool] | None = None
    models_document: object = field(default_factory=lambda: {"object": "list", "data": []})


class RecordingUpstream:
    """A loopback HTTP server standing in for the model gateway."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.requests: list[Recorded] = []
        self.script = UpstreamScript()
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers: dict[str, list[str]] = {}
                for name, value in self.headers.items():
                    headers.setdefault(name.lower(), []).append(value)
                with owner._lock:
                    owner.requests.append(Recorded(self.command, self.path, headers, body))

            def _answer(self) -> None:
                self._record()
                script = owner.script
                if script.handler is not None and script.handler(self):
                    return
                if self.command == "GET" and self.path.split("?")[0] == "/v1/models":
                    payload = json.dumps(script.models_document).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                else:
                    payload = script.body
                    self.send_response(script.status)
                    self.send_header("Content-Type", script.content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _answer  # noqa: N815

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def forwarded(self) -> list[Recorded]:
        with self._lock:
            return list(self.requests)

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()


class RelayProcess:
    """The real ``relay.py`` as a subprocess listening on a free loopback port."""

    def __init__(self, *, upstream: str, token_file: Path, extra_env: dict[str, str] | None = None):
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "RELAY_UPSTREAM": upstream,
            "RELAY_TOKEN_FILE": str(token_file),
            "RELAY_LISTEN_HOST": "127.0.0.1",
            "RELAY_LISTEN_PORT": "0",
            **(extra_env or {}),
        }
        self._process = subprocess.Popen(
            [sys.executable, str(RELAY_SCRIPT)],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self._process.stdout is not None
        announcement = self._process.stdout.readline()
        assert announcement.startswith("relay listening "), announcement
        self.port = int(announcement.rsplit(":", 1)[1])
        self.stderr_text = ""
        self._stopped = False

    def connection(self, timeout: float = 15.0) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = self.connection()
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post_json(
        self, path: str, document: object, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request(
            "POST",
            path,
            body=json.dumps(document).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    def stop(self) -> str:
        """Terminate the relay and return everything it wrote to stderr (idempotent)."""
        if self._stopped:
            return self.stderr_text
        self._stopped = True
        with contextlib.suppress(ProcessLookupError):
            self._process.terminate()
        try:
            _, stderr = self._process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            _, stderr = self._process.communicate()
        self.stderr_text = stderr or ""
        return self.stderr_text


@contextlib.contextmanager
def running_relay(
    upstream: str, token_file: Path, extra_env: dict[str, str] | None = None
) -> Iterator[RelayProcess]:
    relay = RelayProcess(upstream=upstream, token_file=token_file, extra_env=extra_env)
    try:
        yield relay
    finally:
        relay.stop()


def write_token(path: Path, value: str = TOKEN) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path
