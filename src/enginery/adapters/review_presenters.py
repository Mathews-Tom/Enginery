"""Optional display transports. Neither presentation nor feedback grants authority."""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from enginery.evidence.review_rendering import PACKET_MARKER, REVIEW_CSP, escape_terminal

MAX_BYTES = 1024 * 1024
_TIMEOUT = 30.0


class PresenterError(ValueError):
    """A local presenter could not safely complete the requested operation."""


def _environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME") if key in os.environ}


def _private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise PresenterError("presenter state directory must be absolute")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PresenterError("presenter state path must not contain links")
    try:
        info = path.stat()
    except OSError as exc:
        raise PresenterError("presenter private state directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise PresenterError("presenter state directory must already exist with mode 0700")
    if info.st_uid != os.getuid():
        raise PresenterError("presenter state directory must belong to the operator")
    return path


def _execute(argv: tuple[str, ...], environment: dict[str, str]) -> str:
    if any(not item or "\0" in item for item in argv):
        raise PresenterError("presenter argv contains an empty or invalid argument")
    if sum(len(item.encode()) + 1 for item in argv) > MAX_BYTES:
        raise PresenterError("presenter argv exceeds 1 MiB")
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, shell=False,
        )
    except OSError as exc:
        raise PresenterError("presenter executable could not be started") from exc
    output = bytearray()
    total = 0
    deadline = time.monotonic() + _TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                assert stream is not None
                selector.register(stream, selectors.EVENT_READ, stream is process.stdout)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PresenterError("presenter timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(65536, MAX_BYTES + 1 - total))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise PresenterError("presenter output exceeds 1 MiB")
                    if key.data:
                        output.extend(chunk)
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise PresenterError("presenter timed out") from exc
            if code:
                # Tool diagnostics may contain credentials; never echo them.
                raise PresenterError(f"presenter exited with status {code}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PresenterError("presenter output is not UTF-8") from exc


class _PacketMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.marker = False
        self.csp = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"html", "head", "meta", "title", "body", "main", "section", "h1", "h2",
                       "h3", "p", "pre", "ul", "ol", "li", "dl", "dt", "dd", "strong", "code",
                       "table", "thead", "tbody", "tr", "th", "td", "span", "div", "br"}:
            raise PresenterError("packet contains non-Enginery active markup")
        values = dict(attrs)
        if len(values) != len(attrs):
            raise PresenterError("packet contains duplicate attributes")
        if tag == "meta":
            if values == {"name": "enginery-review-packet", "content": PACKET_MARKER}:
                self.marker = True
            elif values == {"http-equiv": "Content-Security-Policy", "content": REVIEW_CSP}:
                self.csp = True
            elif values != {"charset": "utf-8"}:
                raise PresenterError("packet contains unsupported metadata")
        elif any(key not in {"lang", "id", "class"} for key in values):
            raise PresenterError("packet contains active attributes")

    handle_startendtag = handle_starttag


def _packet(path: Path, private_state: Path) -> Path:
    if not path.is_absolute() or path.name != "packet.html":
        raise PresenterError("only a canonical Enginery packet.html may be presented")
    try:
        relative = path.relative_to(private_state / "review")
    except ValueError as exc:
        raise PresenterError("packet must be inside the private review directory") from exc
    if len(relative.parts) != 3 or len(relative.parts[1]) != 64:
        raise PresenterError("packet must use its canonical subject-digest directory")
    if any(char not in "0123456789abcdef" for char in relative.parts[1]):
        raise PresenterError("packet subject digest is invalid")
    for parent in path.parents:
        if parent == private_state:
            break
        _private_directory(parent)
    if path.is_symlink():
        raise PresenterError("packet must not be a link")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PresenterError("packet must be a regular file")
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise PresenterError("packet exceeds 1 MiB")
        parser = _PacketMarkup()
        parser.feed(data.decode("utf-8"))
        parser.close()
    except (OSError, UnicodeError) as exc:
        raise PresenterError("packet is unavailable or invalid UTF-8") from exc
    if not parser.marker or not parser.csp:
        raise PresenterError("packet lacks the Enginery marker or restrictive CSP")
    return path


def _findings(text: str) -> tuple[str, ...]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PresenterError("presenter feedback exceeds 1 MiB")
    if not text.strip():
        return ()
    try:
        value = json.loads(text)
    except RecursionError as exc:
        raise PresenterError("presenter feedback nesting is excessive") from exc
    except json.JSONDecodeError:
        if text.lstrip().startswith(("{", "[")):
            raise PresenterError("presenter feedback contains malformed JSON") from None
        return tuple(escape_terminal(line) for line in text.splitlines() if line.strip())
    result: list[str] = []
    def collect(item: object) -> None:
        if isinstance(item, str) and item.strip():
            result.append(escape_terminal(item))
        elif isinstance(item, list):
            for entry in item:
                collect(entry)
        elif isinstance(item, dict):
            # Allowlisted feedback fields only: approval/action/principal fields are ignored.
            for field in ("findings", "annotations", "prompts", "feedback", "prompt",
                          "text", "message"):
                if field in item:
                    collect(item[field])
    try:
        collect(value)
    except RecursionError as exc:
        raise PresenterError("presenter feedback nesting is excessive") from exc
    return tuple(result)


@dataclass(frozen=True, slots=True)
class LavishPresenter:
    executable: str
    private_state_dir: Path
    port: int
    no_open: bool = True

    def __post_init__(self) -> None:
        if not self.executable or self.executable.startswith("-"):
            raise PresenterError("Lavish executable must be explicitly configured")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise PresenterError("Lavish port must be between 1 and 65535")
        _private_directory(self.private_state_dir)

    def _run(self, *arguments: str) -> str:
        _private_directory(self.private_state_dir)
        environment = _environment()
        environment.update(LAVISH_AXI_TELEMETRY="0", LAVISH_AXI_STATE_DIR=str(self.private_state_dir),
                           LAVISH_AXI_PORT=str(self.port))
        if self.no_open:
            environment["LAVISH_AXI_NO_OPEN"] = "1"
        return _execute((self.executable, *arguments), environment)

    def open(self, packet_path: Path) -> None:
        self._run(str(_packet(packet_path, self.private_state_dir)))

    def poll(self, packet_path: Path) -> tuple[str, ...]:
        return _findings(self._run("poll", str(_packet(packet_path, self.private_state_dir))))

    def end(self, packet_path: Path) -> None:
        self._run("end", str(_packet(packet_path, self.private_state_dir)))


@dataclass(frozen=True, slots=True)
class GhAxiView:
    executable: str

    def _view(self, command: str, repository: str, number: int) -> str:
        if not self.executable or self.executable.startswith("-"):
            raise PresenterError("gh-axi executable must be explicitly configured")
        parts = repository.split("/")
        if len(parts) != 2 or any(not part or not part[0].isalnum() or any(
            not (char.isascii() and (char.isalnum() or char in "-_.")) for char in part
        ) for part in parts):
            raise PresenterError("repository must be an owner/name pair")
        if isinstance(number, bool) or number < 1:
            raise PresenterError("pull request number must be positive")
        return escape_terminal(_execute(
            (self.executable, "pr", command, str(number), "--repo", repository), _environment()
        ))

    def pr_view(self, repository: str, number: int) -> str:
        return self._view("view", repository, number)

    def checks(self, repository: str, number: int) -> str:
        return self._view("checks", repository, number)
