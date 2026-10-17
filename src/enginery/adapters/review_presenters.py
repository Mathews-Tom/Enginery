"""Optional display transports. Neither presentation nor feedback grants authority."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from enginery.adapters.subprocess_output import (
    SubprocessOutputError,
    execute_output,
)
from enginery.evidence.review_rendering import PACKET_MARKER, REVIEW_CSP, escape_terminal

MAX_BYTES = 1024 * 1024


class PresenterError(ValueError):
    """A local presenter could not safely complete the requested operation."""


def _environment() -> dict[str, str]:
    return {
        key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME") if key in os.environ
    }


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
    try:
        return execute_output(argv, environment)
    except SubprocessOutputError as error:
        raise PresenterError(str(error)) from error


class _PacketMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.marker = False
        self.csp = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {
            "html",
            "head",
            "meta",
            "title",
            "body",
            "main",
            "section",
            "h1",
            "h2",
            "h3",
            "p",
            "pre",
            "ul",
            "ol",
            "li",
            "dl",
            "dt",
            "dd",
            "strong",
            "code",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "span",
            "div",
            "br",
        }:
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


def _toon_value(token: str) -> str:
    token = token.strip()
    if token.startswith('"'):
        try:
            value = json.loads(token)
        except ValueError as error:
            raise PresenterError("Lavish prompt text is malformed") from error
        if not isinstance(value, str):
            raise PresenterError("Lavish prompt text is malformed")
        return value
    return token


def _toon_row(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for char in line:
        if quoted:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            current.append(char)
        elif char == ",":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
    if quoted:
        raise PresenterError("Lavish prompt row is malformed")
    cells.append("".join(current))
    return [_toon_value(cell) for cell in cells]


def _findings(text: str) -> tuple[str, ...]:
    """Accept prompt text only, never session instructions or DOM snapshots."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PresenterError("presenter feedback exceeds 1 MiB")
    if not text.strip():
        return ()
    if text.lstrip().startswith("{"):
        try:
            document = json.loads(text)
        except (ValueError, RecursionError) as error:
            raise PresenterError("presenter feedback contains malformed JSON") from error
        if not isinstance(document, dict):
            raise PresenterError("presenter feedback must be an object")
        if document.get("artifact_failures"):
            raise PresenterError("Lavish artifact failures prevent review; repair the packet")
        prompts = document.get("prompts", [])
        if not isinstance(prompts, list):
            raise PresenterError("Lavish prompts must be a list")
        return tuple(
            escape_terminal(item["prompt"])
            for item in prompts
            if isinstance(item, dict)
            and isinstance(item.get("prompt"), str)
            and item["prompt"].strip()
        )
    # TOON uses tables for primitive objects and lists for nested targets/attachments.
    result: list[str] = []
    mode = ""
    fields: list[str] = []
    for line in text.split("\n"):
        if line and not line[0].isspace():
            if re.match(r"artifact_failures\[[1-9][0-9]*\]", line):
                raise PresenterError("Lavish artifact failures prevent review; repair the packet")
            table = re.fullmatch(r"prompts\[[0-9]+\]\{([^}]*)\}:", line)
            if table:
                mode, fields = "table", table.group(1).split(",")
                if "prompt" not in fields:
                    raise PresenterError("Lavish prompts lack prompt text")
            elif re.fullmatch(r"prompts\[[0-9]+\]:", line):
                mode = "list"
            else:
                mode = ""
            continue
        if mode == "table" and line.startswith("  ") and not line.startswith("   "):
            row = _toon_row(line[2:])
            if len(row) != len(fields):
                raise PresenterError("Lavish prompt row is malformed")
            value = row[fields.index("prompt")]
        elif mode == "list":
            match = re.fullmatch(r"  - prompt: (.*)|    prompt: (.*)", line)
            if match is None:
                continue
            value = _toon_value(match.group(1) or match.group(2) or "")
        else:
            continue
        if value.strip():
            result.append(escape_terminal(value))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class LavishPresenter:
    executable: str
    private_state_dir: Path
    port: int
    no_open: bool = True
    packet_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.executable or self.executable.startswith("-"):
            raise PresenterError("Lavish executable must be explicitly configured")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise PresenterError("Lavish port must be between 1 and 65535")
        _private_directory(self.private_state_dir)

    def _run(self, *arguments: str) -> str:
        _private_directory(self.private_state_dir)
        environment = _environment()
        environment.update(
            LAVISH_AXI_TELEMETRY="0",
            LAVISH_AXI_HOST="127.0.0.1",
            LAVISH_AXI_STATE_DIR=str(self.private_state_dir),
            LAVISH_AXI_PORT=str(self.port),
        )
        if self.no_open:
            environment["LAVISH_AXI_NO_OPEN"] = "1"
        return _execute((self.executable, *arguments), environment)

    def open(self, packet_path: Path) -> None:
        self._run(str(_packet(packet_path, self.packet_root or self.private_state_dir)))

    def poll(self, packet_path: Path) -> tuple[str, ...]:
        return _findings(
            self._run("poll", str(_packet(packet_path, self.packet_root or self.private_state_dir)))
        )

    def end(self, packet_path: Path) -> None:
        self._run("end", str(_packet(packet_path, self.packet_root or self.private_state_dir)))


@dataclass(frozen=True, slots=True)
class GhAxiView:
    executable: str

    def _view(self, command: str, repository: str, number: int) -> str:
        if not self.executable or self.executable.startswith("-"):
            raise PresenterError("gh-axi executable must be explicitly configured")
        parts = repository.split("/")
        if len(parts) != 2 or any(
            not part
            or not part[0].isalnum()
            or any(not (char.isascii() and (char.isalnum() or char in "-_.")) for char in part)
            for part in parts
        ):
            raise PresenterError("repository must be an owner/name pair")
        if isinstance(number, bool) or number < 1:
            raise PresenterError("pull request number must be positive")
        return escape_terminal(
            _execute(
                (self.executable, "pr", command, str(number), "--repo", repository), _environment()
            )
        )

    def pr_view(self, repository: str, number: int) -> str:
        return self._view("view", repository, number)

    def checks(self, repository: str, number: int) -> str:
        return self._view("checks", repository, number)
