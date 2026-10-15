"""Host-side support for ``run_factory_gate.py``: stubs, fixtures, observers and attempt launching.

Everything here is self-contained source that runs both from the repository and from a copy of
``scripts/`` next to an installed ``enginery`` wheel; it imports nothing from ``tests/``.

Nothing in this module holds a real credential. Canary secrets are random strings that identify
nothing; the only real secret the gate can meet is a gateway token file an operator names, which is
read in-process solely to scrub it from every artifact and is never printed or copied.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enginery.adapters.oci.engine import EngineCommandError, OciEngine, engine_environment
from enginery.adapters.oci.launch import container_name_for, store_arguments, store_names_for
from enginery.adapters.runner_launch import LAUNCHER_MODULE, worker_environment
from enginery.application.worker_environment import LAUNCHER_KEYS, build_worker_environment
from enginery.domain.errors import EngineryError
from enginery.domain.runner import (
    LaunchSpec,
    PermissionMapping,
    RunnerProfile,
    RunnerReceipt,
    expected_network,
    profile_violations,
)
from enginery.engine.task_repository import materialize_task_local_repository

PROBE_SOURCE = Path(__file__).resolve().with_name("factory_gate_probe.py")
PROBE_IN_CONTAINER = "/out/factory_gate_probe.py"
GATE_LABEL = "dev.enginery.gate"
REDACTED = "<redacted:{label}>"

GATE_PERMISSIONS = PermissionMapping(
    harness="gate-probe",
    name="gate-probe-no-harness",
    arguments=(),
    unconditional_bypass=False,
    rationale="no harness runs; the attempt program runs under the isolated container alone",
)


# --- hashing and scrubbing -------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_tree(root: Path) -> dict[str, str]:
    """Content, mode, size and mtime of every entry under ``root`` (files read, never followed)."""
    snapshot: dict[str, str] = {}
    if not root.exists() and not root.is_symlink():
        return {".": "absent"}
    entries = [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]
    for entry in entries:
        relative = "." if entry == root else str(entry.relative_to(root))
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = f"link:{os.readlink(entry)}"
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = f"dir:{stat.S_IMODE(info.st_mode):o}"
        elif stat.S_ISREG(info.st_mode):
            snapshot[relative] = (
                f"file:{stat.S_IMODE(info.st_mode):o}:{info.st_size}:{info.st_mtime_ns}:"
                f"{sha256_file(entry)}"
            )
        else:
            snapshot[relative] = f"special:{stat.S_IFMT(info.st_mode):o}"
    return snapshot


def diff_snapshots(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    changes: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            kind = "added" if key not in before else "removed" if key not in after else "changed"
            changes.append(f"{kind}: {key}")
    return changes


def tree_digest(snapshot: Mapping[str, str]) -> str:
    return sha256_text(json.dumps(sorted(snapshot.items()), separators=(",", ":")))


class Scrubber:
    """Replaces every registered secret value in text and JSON with a labelled placeholder."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register(self, label: str, value: str) -> None:
        if len(value) >= 8:
            self._values[value] = label

    def text(self, value: str) -> str:
        for secret, label in self._values.items():
            if secret in value:
                value = value.replace(secret, REDACTED.format(label=label))
        return value

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(key)): self.data(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.data(item) for item in value]
        return value

    def contains(self, value: str) -> list[str]:
        """Labels of registered secrets present in ``value`` (never the secrets themselves)."""
        return sorted({label for secret, label in self._values.items() if secret in value})

    @property
    def labels(self) -> list[str]:
        return sorted(set(self._values.values()))


# --- git -------------------------------------------------------------------------------------


def git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Gate Fixture",
        "GIT_AUTHOR_EMAIL": "gate@enginery.invalid",
        "GIT_COMMITTER_NAME": "Gate Fixture",
        "GIT_COMMITTER_EMAIL": "gate@enginery.invalid",
    }


def git(
    arguments: Sequence[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=git_environment(),
        timeout=120,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments[:3])} failed: {completed.stderr.strip()}")
    return completed


def bare_refs(origin: Path) -> dict[str, str]:
    out = git(["--git-dir", str(origin), "for-each-ref", "--format=%(refname) %(objectname)"])
    return dict(line.split(" ", 1) for line in out.stdout.splitlines() if line.strip())


# --- recording listeners ---------------------------------------------------------------------


class RecordingListener:
    """A TCP listener that records every connection it accepts and answers with a marker."""

    def __init__(self, name: str, bind_host: str) -> None:
        self.name = name
        self.bind_host = bind_host
        self.port = 0
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: list[dict[str, Any]] = []

    def start(self) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.bind_host, 0))
        listener.listen(64)
        listener.settimeout(0.2)
        self._socket = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(
            target=self._serve, name=f"listener-{self.name}", daemon=True
        )
        self._thread.start()
        return self.port

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, peer = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1.0)
                received = b""
                with contextlib.suppress(OSError):
                    received = connection.recv(256)
                with self._lock:
                    self._connections.append(
                        {
                            "listener": self.name,
                            "peer": peer[0],
                            "at": time.time(),
                            "first_bytes": received[:80].decode("latin-1"),
                        }
                    )
                body = f"gate-stub-reply:{self.name}".encode()
                with contextlib.suppress(OSError):
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + body
                    )

    def connections(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._connections)

    def count(self) -> int:
        with self._lock:
            return len(self._connections)

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class StubNetwork:
    """Stub gateway, stub broker and control listeners on host loopback and 0.0.0.0."""

    NAMES = ("gateway", "broker", "control")

    def __init__(self) -> None:
        self.listeners: dict[str, RecordingListener] = {}

    def start(self) -> None:
        for name in self.NAMES:
            for scope, bind in (("loopback", "127.0.0.1"), ("any", "0.0.0.0")):
                listener = RecordingListener(f"{name}-{scope}", bind)
                listener.start()
                self.listeners[f"{name}-{scope}"] = listener

    def ports(self, name: str) -> dict[str, int]:
        return {scope: self.listeners[f"{name}-{scope}"].port for scope in ("loopback", "any")}

    def counts(self) -> dict[str, int]:
        return {key: listener.count() for key, listener in sorted(self.listeners.items())}

    def forbidden_count(self) -> int:
        """Connections to the stub gateway and broker: every one is an escape."""
        return sum(
            listener.count()
            for key, listener in self.listeners.items()
            if key.startswith(("gateway-", "broker-"))
        )

    def all_ports(self, names: Sequence[str] = ("gateway", "broker")) -> list[int]:
        return [port for name in names for port in self.ports(name).values()]

    def stop(self) -> None:
        for listener in self.listeners.values():
            listener.stop()


# --- loopback upstream (stands in for the model gateway behind the relay) --------------------

STUB_MODELS = (
    "anthropic/claude-gate-1",
    "openai-codex/gpt-gate",
    "google-antigravity/gemini-gate",
    "openai/gpt-4o",
    "claude-gate-bare",
)


@dataclass(slots=True)
class RecordedRequest:
    seq: int
    method: str
    path: str
    headers: dict[str, str]
    authorization_scheme: str | None
    authorization_matches_gateway: bool
    body_text: str
    probe: bool

    def to_state(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "method": self.method,
            "path": self.path,
            "header_names": sorted(self.headers),
            "authorization_scheme": self.authorization_scheme,
            "authorization_is_gateway_bearer": self.authorization_matches_gateway,
            "body_bytes": len(self.body_text),
            "probe": self.probe,
        }


def _anthropic_frames(model: str, text: str) -> list[tuple[str, dict[str, Any]]]:
    message: dict[str, Any] = {
        "id": "msg_gate_stub",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    return [
        ("message_start", {"type": "message_start", "message": message}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


def _pi_native_frames(model_id: str, text: str) -> list[tuple[str, dict[str, Any]]]:
    usage = {
        "input": 3,
        "output": 1,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 4,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }
    content = [{"type": "text", "text": text}]
    message = {
        "role": "assistant",
        "content": content,
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": model_id.split("/", 1)[-1],
        "usage": usage,
        "stopReason": "stop",
        "timestamp": 0,
    }
    empty = {**message, "content": [], "stopReason": "stop"}
    events: list[dict[str, Any]] = [
        {"type": "start", "partial": empty},
        {"type": "text_start", "contentIndex": 0, "partial": message},
        {"type": "text_delta", "contentIndex": 0, "delta": text, "partial": message},
        {"type": "text_end", "contentIndex": 0, "content": text, "partial": message},
        {"type": "done", "reason": "stop", "message": message},
    ]
    return [("message", event) for event in events]


class UpstreamStub:
    """A recording loopback HTTP server that stands in for the gateway behind the relay.

    It answers ``GET /v1/models``, ``POST /v1/messages`` (Anthropic SSE) and ``POST /v1/pi/stream``
    (pi-native SSE) with minimal valid bodies, and records every request it receives so a
    denied request is provable by its absence.
    """

    def __init__(self, expected_bearer: str, *, reply_text: str = "OK") -> None:
        self._expected = f"Bearer {expected_bearer}"
        self.reply_text = reply_text
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self, bind_host: str = "0.0.0.0") -> int:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                del args

            def _body(self) -> str:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length).decode("utf-8", "replace") if length else ""

            def _record(self, body: str) -> RecordedRequest:
                authorization = self.headers.get("Authorization")
                headers = {name.lower(): value for name, value in self.headers.items()}
                headers.pop("authorization", None)
                with outer._lock:
                    request = RecordedRequest(
                        seq=len(outer.requests) + 1,
                        method=self.command,
                        path=self.path,
                        headers=headers,
                        authorization_scheme=(
                            authorization.split(" ", 1)[0] if authorization else None
                        ),
                        authorization_matches_gateway=authorization == outer._expected,
                        body_text=body[:16384],
                        probe=self.path.startswith("/__gate_probe"),
                    )
                    outer.requests.append(request)
                return request

            def _send(self, status: int, payload: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _sse(self, frames: list[tuple[str, dict[str, Any]]]) -> None:
                text = "".join(
                    f"event: {name}\ndata: {json.dumps(item)}\n\n" for name, item in frames
                )
                self._send(200, text.encode(), "text/event-stream")

            def do_GET(self) -> None:
                request = self._record("")
                route = request.path.partition("?")[0]
                if route == "/v1/models":
                    data = [{"id": model, "object": "model"} for model in STUB_MODELS]
                    self._send(
                        200,
                        json.dumps({"object": "list", "data": data}).encode(),
                        "application/json",
                    )
                elif request.probe:
                    self._send(200, b"{}", "application/json")
                else:
                    self._send(404, b'{"error":"gate-stub-not-found"}', "application/json")

            def do_POST(self) -> None:
                body = self._body()
                request = self._record(body)
                route = request.path.partition("?")[0]
                try:
                    document = json.loads(body) if body else {}
                except ValueError:
                    document = {}
                if not isinstance(document, dict):
                    document = {}
                if route == "/v1/messages":
                    model = str(document.get("model", "anthropic/claude-gate-1"))
                    if document.get("stream") is True:
                        self._sse(_anthropic_frames(model, outer.reply_text))
                    else:
                        message = _anthropic_frames(model, outer.reply_text)[0][1]["message"]
                        message = {
                            **message,
                            "content": [{"type": "text", "text": outer.reply_text}],
                            "stop_reason": "end_turn",
                        }
                        self._send(200, json.dumps(message).encode(), "application/json")
                elif route == "/v1/pi/stream":
                    model = str(document.get("modelId") or "anthropic/claude-gate-1")
                    self._sse(_pi_native_frames(model, outer.reply_text))
                else:
                    self._send(404, b'{"error":"gate-stub-not-found"}', "application/json")

        server = http.server.ThreadingHTTPServer((bind_host, 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def snapshot(self, *, include_probes: bool = False) -> list[RecordedRequest]:
        with self._lock:
            return [r for r in self.requests if include_probes or not r.probe]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# --- host fixture ----------------------------------------------------------------------------


@dataclass(slots=True)
class Canary:
    label: str
    value: str
    path: Path | None
    name: str | None


@dataclass(slots=True)
class HostFixture:
    root: Path
    canaries: dict[str, Canary]
    ledger_path: Path
    artifacts_dir: Path
    config_dir: Path
    engine_dir: Path
    holdouts_dir: Path
    home_dir: Path
    origin: Path
    repo: Path
    receipts: Path
    work: Path
    markers: Path
    base_revision: str
    protected_paths: list[str] = field(default_factory=list)
    host_looking_paths: list[str] = field(default_factory=list)

    def canary_values(self) -> dict[str, str]:
        return {label: canary.value for label, canary in self.canaries.items()}

    def canary_names(self) -> dict[str, str]:
        return {
            label: canary.name for label, canary in self.canaries.items() if canary.name is not None
        }

    def watched_canary_files(self) -> list[Path]:
        return [c.path for c in self.canaries.values() if c.path is not None]


def _canary(prefix: str) -> str:
    return f"gatecanary-{prefix}-{secrets.token_hex(12)}"


def build_fixture(root: Path) -> HostFixture:
    """Create the host tree a worker must never reach: ledger, artifacts, config, engine, SCM."""
    from enginery.ledger.service import LedgerService

    root.mkdir(parents=True, exist_ok=True)
    canaries: dict[str, Canary] = {}

    def register(label: str, path: Path | None, name: str | None = None) -> str:
        value = _canary(label)
        canaries[label] = Canary(label, value, path, name or (path.name if path else None))
        return value

    ledger_dir = root / "ledger"
    ledger_dir.mkdir()
    ledger_path = ledger_dir / "enginery-ledger.sqlite3"
    service = LedgerService.open(ledger_path, artifact_store_root=root / "ledger-artifacts")
    service.close()
    canary_value = register("ledger", ledger_path)
    connection = sqlite3.connect(ledger_path)
    try:
        connection.execute("CREATE TABLE gate_canary (value TEXT NOT NULL)")
        connection.execute("INSERT INTO gate_canary VALUES (?)", (canary_value,))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    artifacts = root / "artifacts"
    artifacts.mkdir()
    artifact = artifacts / f"artifact-{secrets.token_hex(6)}.bin"
    artifact.write_text(register("artifact", artifact) + "\n", encoding="utf-8")

    config = root / "config"
    config.mkdir()
    for label, name in (("broker-token", "broker.token"), ("gateway-token", "gateway.token")):
        path = config / name
        path.write_text(register(label, path) + "\n", encoding="utf-8")
        path.chmod(0o600)

    engine_dir = root / "installed-engine"
    (engine_dir / "enginery").mkdir(parents=True)
    (engine_dir / "enginery" / "__init__.py").write_text(
        "# fake installed engine\n__version__ = '0.0.0-gate'\n", encoding="utf-8"
    )
    engine_marker = engine_dir / "enginery" / "CANARY.txt"
    engine_marker.write_text(register("engine-install", engine_marker) + "\n", encoding="utf-8")
    (engine_dir / "sitecustomize.py").write_text("# gate fixture\n", encoding="utf-8")

    holdouts = root / "holdouts"
    holdouts.mkdir()
    holdout = holdouts / f"holdout-{secrets.token_hex(6)}.json"
    holdout.write_text(json.dumps({"answer": register("holdout", holdout)}), encoding="utf-8")

    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".config" / "gh").mkdir(parents=True)
    ssh_key = home / ".ssh" / "id_ed25519"
    ssh_key.write_text(
        f"-----BEGIN FAKE KEY-----\n{register('ssh-key', ssh_key)}\n-----END FAKE KEY-----\n",
        encoding="utf-8",
    )
    ssh_key.chmod(0o600)
    gh_hosts = home / ".config" / "gh" / "hosts.yml"
    gh_hosts.write_text(
        f"github.com:\n  oauth_token: {register('gh-token', gh_hosts)}\n  user: gate\n",
        encoding="utf-8",
    )
    gh_hosts.chmod(0o600)
    credentials = home / ".git-credentials"
    credentials.write_text(
        f"https://gate:{register('git-credentials', credentials)}@github.com\n", encoding="utf-8"
    )
    credentials.chmod(0o600)

    origin = root / "origin.git"
    git(["init", "--bare", "-b", "main", str(origin)])
    repo = root / "repo"
    git(["init", "-b", "main", str(repo)])
    (repo / "README.md").write_text("# gate fixture repository\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(["add", "-A"], cwd=repo)
    git(["commit", "-q", "-m", "base"], cwd=repo)
    git(["remote", "add", "origin", str(origin)], cwd=repo)
    git(["push", "-q", "origin", "main"], cwd=repo)
    base = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    receipts = root / "receipts"
    receipts.mkdir()
    work = root / "work"
    work.mkdir()
    markers = root / "markers"
    markers.mkdir()

    user = os.environ.get("USER", "operator")
    fixture = HostFixture(
        root=root,
        canaries=canaries,
        ledger_path=ledger_path,
        artifacts_dir=artifacts,
        config_dir=config,
        engine_dir=engine_dir,
        holdouts_dir=holdouts,
        home_dir=home,
        origin=origin,
        repo=repo,
        receipts=receipts,
        work=work,
        markers=markers,
        base_revision=base,
    )
    fixture.protected_paths = [
        str(ledger_path),
        str(artifact),
        str(config / "broker.token"),
        str(config / "gateway.token"),
        str(engine_dir / "enginery" / "__init__.py"),
        str(engine_marker),
        str(holdout),
        str(ssh_key),
        str(gh_hosts),
        str(credentials),
        str(origin / "HEAD"),
        str(repo / ".git" / "config"),
    ]
    fixture.host_looking_paths = [
        str(ledger_path),
        f"/Users/{user}/.ssh/id_ed25519",
        f"/Users/{user}/.config/gh/hosts.yml",
        f"/home/{user}/.ssh/id_ed25519",
        f"/home/{user}/.config/gh/hosts.yml",
        f"/Users/{user}/.enginery/ledger.sqlite3",
        f"/Users/{user}/.enginery/broker.token",
    ]
    return fixture


def new_task_workspace(fixture: HostFixture, name: str) -> Path:
    """A fresh task-local clone of the fixture repository, exactly as a real task gets it."""
    workspace = fixture.work / name / "ws"
    materialize_task_local_repository(
        repository_path=fixture.repo,
        workspace_path=workspace,
        base_revision=fixture.base_revision,
    )
    return workspace


# --- engine helpers --------------------------------------------------------------------------


class ContainerObserver:
    """Host-side observation of one container: the engine's event stream and state samples."""

    def __init__(self, engine: OciEngine, name: str) -> None:
        self.engine = engine
        self.name = name
        self.events: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self._process: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        argv = [self.engine.executable]
        if self.engine.context is not None:
            argv += ["--context", self.engine.context]
        argv += ["events", "--filter", f"container={self.name}", "--format", "{{json .}}"]
        self._process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=engine_environment(),
        )
        self._threads = [
            threading.Thread(target=self._read_events, daemon=True),
            threading.Thread(target=self._sample_states, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        time.sleep(0.5)

    def _read_events(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            with contextlib.suppress(ValueError):
                document = json.loads(line)
                if isinstance(document, dict):
                    self.events.append(
                        {
                            "action": document.get("Action"),
                            "type": document.get("Type"),
                            "exit_code": (
                                document.get("Actor", {}).get("Attributes", {}) or {}
                            ).get("exitCode"),
                            "time_nano": document.get("timeNano"),
                        }
                    )

    def _sample_states(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                completed = self.engine.run_cli(
                    ("container", "inspect", "--format", "{{json .State}}", self.name),
                    timeout=10,
                )
                if completed.returncode == 0:
                    document = json.loads(completed.stdout)
                    if isinstance(document, dict):
                        self.states.append(
                            {
                                "status": document.get("Status"),
                                "oom_killed": document.get("OOMKilled"),
                                "exit_code": document.get("ExitCode"),
                            }
                        )
            self._stop.wait(0.2)

    def stop(self) -> dict[str, Any]:
        time.sleep(1.0)
        self._stop.set()
        if self._process is not None:
            self._process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(5)
        for thread in self._threads:
            thread.join(timeout=5)
        return {
            "events": self.events,
            "event_actions": [event["action"] for event in self.events],
            "state_samples": len(self.states),
            "oom_killed_in_samples": any(
                sample.get("oom_killed") is True for sample in self.states
            ),
            "last_state": self.states[-1] if self.states else None,
        }


@dataclass(slots=True)
class AttemptResult:
    name: str
    mode: str
    exit_code: int
    duration_seconds: float
    launcher_stderr: str
    container_name: str
    operation_id: str
    command_line: str
    spec_ref: str
    receipt: RunnerReceipt | None
    receipt_ref: str | None
    violations: tuple[str, ...]
    result: dict[str, Any] | None
    obs: dict[str, Any] | None
    workspace: Path
    out: Path
    container_gone: bool
    observer: dict[str, Any] | None = None
    timed_out: bool = False
    outcome: dict[str, Any] | None = None
    attempt_network: str = "none"

    @property
    def operations(self) -> list[dict[str, Any]]:
        return list(self.obs.get("operations", [])) if self.obs else []

    def output_text(self) -> str:
        return str(self.result.get("output", "")) if self.result else ""


def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from a worker-controlled path, never following a link or blocking on a FIFO."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            document = json.loads(handle.read(64 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)
    return document if isinstance(document, dict) else None


def launcher_environment() -> dict[str, str]:
    return build_worker_environment(os.environ, LAUNCHER_KEYS)


def launch_command(spec: LaunchSpec, inner: Sequence[str]) -> list[str]:
    encoded = json.dumps(spec.to_state(), sort_keys=True, separators=(",", ":"))
    return [sys.executable, "-m", LAUNCHER_MODULE, "--spec", encoded, "--", *inner]


def display_command(spec_ref: str, inner: Sequence[str]) -> str:
    return " ".join(
        [Path(sys.executable).name, "-m", LAUNCHER_MODULE, "--spec", f"@{spec_ref}", "--", *inner]
    )


def create_attempt_store(engine: OciEngine, spec: LaunchSpec) -> None:
    """Create and start the attempt's store container, so the capped volumes a worker joins exist.

    A worker created from ``create_arguments`` mounts the attempt's two volumes by name; without
    the store container the engine would create plain, uncapped volumes for it.
    """
    created = engine.run_cli(store_arguments(spec), timeout=120)
    if created.returncode != 0:
        raise RuntimeError(f"the store container could not be created: {created.stderr.strip()}")
    engine.start(store_names_for(spec.container_name).holder)


def remove_attempt_store(engine: OciEngine, spec: LaunchSpec) -> None:
    """Remove the attempt's store container and its two volumes; whatever is absent is skipped."""
    names = store_names_for(spec.container_name)
    with contextlib.suppress(EngineryError):
        engine.remove(names.holder, force=True)
    for volume in (names.workspace_volume, names.out_volume):
        with contextlib.suppress(EngineryError):
            engine.volume_remove(volume)


def make_spec(
    *,
    operation_id: str,
    image: str,
    profile: RunnerProfile,
    workspace: Path,
    out: Path,
    receipt_path: Path,
    time_budget_seconds: int,
    docker_context: str | None,
    env: Sequence[tuple[str, str]] = (),
    seed_files: Sequence[tuple[str, str]] = (),
    bundle_branch: str | None = None,
    bundle_base: str | None = None,
    permissions: PermissionMapping = GATE_PERMISSIONS,
    harness: str = "gate-probe",
) -> LaunchSpec:
    return LaunchSpec(
        operation_id=operation_id,
        container_name=container_name_for(operation_id),
        image=image,
        profile=profile,
        harness=harness,
        workspace_path=str(workspace),
        out_path=str(out),
        receipt_path=str(receipt_path),
        result_name="result.json",
        time_budget_seconds=time_budget_seconds,
        permissions=permissions,
        env=worker_environment(tuple(env)),
        seed_files=tuple(seed_files),
        bundle_branch=bundle_branch,
        bundle_base=bundle_base,
        docker_context=docker_context,
    )


def read_receipt(path: Path) -> tuple[RunnerReceipt | None, dict[str, Any] | None]:
    document = read_json(path)
    if document is None:
        return None, None
    try:
        return RunnerReceipt.from_state(document), document
    except Exception:
        return None, document


PARAMS_IN_CONTAINER = "/out/params.json"
OBS_LINE_PREFIX = "GATE_OBS "
OBS_FILE_IN_WORKSPACE = ".git/gate-obs.json"
"""Where the probe leaves a second copy of its observation: inside the workspace, which the
launcher reads back to the host, so the observation survives a supervisor that was killed."""


def probe_seed_files(params: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The attempt program and its attempt list, as files the in-container supervisor writes.

    ``/out`` is a kernel-capped volume the host never mounts, so nothing can be staged into it
    from the host: the launch spec carries the two files and the supervisor writes them before
    it runs the command.
    """
    return (
        (PROBE_IN_CONTAINER, PROBE_SOURCE.read_text(encoding="utf-8")),
        (PARAMS_IN_CONTAINER, json.dumps(params, sort_keys=True)),
    )


def read_probe_observation(
    result: Mapping[str, Any] | None, workspace: Path
) -> dict[str, Any] | None:
    """What the probe reported: the copy it left in the workspace the launcher read back, else
    its last ``GATE_OBS`` line in the collected result.

    The workspace copy comes first because the result file is replaceable by the worker (a
    forged or replayed result carries another attempt's output); the workspace copy is removed
    before each launch, so it can only be this attempt's.
    """
    document = read_json(workspace / OBS_FILE_IN_WORKSPACE)
    if document is not None:
        return document
    output = str(result.get("output", "")) if result else ""
    for line in reversed(output.splitlines()):
        if line.startswith(OBS_LINE_PREFIX):
            try:
                parsed = json.loads(line[len(OBS_LINE_PREFIX) :])
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def violations_of(
    profile: RunnerProfile, receipt: RunnerReceipt | None, *, relay: bool = False
) -> tuple[str, ...]:
    """The product's ``profile_violations`` for a receipt, against the one network it was owed.

    ``relay`` says whether the attempt was promised model access (its own per-attempt internal
    network); every other attempt is owed ``none``.
    """
    if receipt is None:
        return ("no runner receipt was written",)
    network = expected_network(profile, receipt.operation_id, relay=relay)
    return profile_violations(profile, receipt.effective, network=network)


def container_exists(engine: OciEngine, name: str) -> bool:
    return engine.container_exists(name)


def list_engine_names(engine: OciEngine, arguments: Sequence[str], template: str) -> list[str]:
    """The names one engine listing prints; a failed listing raises, it never reads as 'none'.

    A stray check that treated a failed ``docker ps`` or ``network ls`` as an empty list would
    pass exactly when the engine could not be asked.
    """
    completed = engine.run_cli((*arguments, "--format", template), timeout=60)
    if completed.returncode != 0:
        raise EngineCommandError(
            f"the engine cannot list {' '.join(arguments)}: {completed.stderr.strip()[-200:]}"
        )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def iter_enginery_containers(engine: OciEngine) -> list[str]:
    """Every ``enginery-*`` container, running or not."""
    names = list_engine_names(engine, ("ps", "--all"), "{{.Names}}")
    return [name for name in names if name.startswith("enginery-")]


def _stop_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Ask ``process``'s group to terminate, kill it if it ignores that; return what it wrote."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, 15)
    try:
        return process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, 9)
        return process.communicate()


def run_with_deadline(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    deadline_seconds: float,
    stop: Callable[[], None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run ``argv`` in its own process group; kill the whole group when the deadline passes."""
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if stop is not None:
            stop()
        stdout, stderr = _stop_group(process)
    except BaseException:
        # An interrupted gate must not orphan the launcher it started in its own session: stop
        # the group and wait, so the launcher removes its own worker, store and network first.
        _stop_group(process)
        raise
    return (
        subprocess.CompletedProcess(list(argv), process.returncode, stdout or "", stderr or ""),
        timed_out,
    )
