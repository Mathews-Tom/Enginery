#!/usr/bin/env python3
"""A real, minimal, stdlib-only HTTP service fixture for Stage 3's
controlled local deployment target.

Reads its configuration once at process startup and never hot-reloads:
every deploy or rollback is a full process replacement (a new subprocess
bound to the new configuration), matching a real deployment's "new
process, new revision" semantics rather than an in-place mutation a
fixture could fake.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_DEFECT_MODES = frozenset({"none", "increment_off_by_one", "health_degraded"})


@dataclass(frozen=True)
class ServiceConfig:
    revision: str
    defect_mode: str

    @classmethod
    def load(cls, path: str) -> ServiceConfig:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        revision = str(raw["revision"])
        defect_mode = str(raw.get("defect_mode", "none"))
        if not revision.strip():
            raise ValueError("config revision must be non-blank")
        if defect_mode not in _DEFECT_MODES:
            raise ValueError(f"unknown defect_mode: {defect_mode!r}")
        return cls(revision=revision, defect_mode=defect_mode)


def make_handler(config: ServiceConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # silence default stderr access logging

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/version":
                self._send_json(200, {"revision": config.revision, "pid": os.getpid()})
            elif self.path == "/health":
                status = "unhealthy" if config.defect_mode == "health_degraded" else "healthy"
                self._send_json(200, {"status": status})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/increment":
                self._send_json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw_body)
                value = int(body["value"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self._send_json(400, {"error": "value must be an integer"})
                return
            offset = 2 if config.defect_mode == "increment_off_by_one" else 1
            self._send_json(200, {"result": value + offset})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 3 controlled local service fixture.")
    parser.add_argument("--config", required=True, help="path to a service config JSON file")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    config = ServiceConfig.load(args.config)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(config))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
