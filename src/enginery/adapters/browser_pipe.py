"""Approved headless-shell transport over anonymous CDP pipes, never a debug socket.

Trusted-local execution is not OS containment. Every launch checks the entire
operator-pinned distribution tree, uses a fresh profile, disables background
network features, and intercepts page requests before navigating. No sandbox
weakening flags or ambient browser profile are used.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import selectors
import signal
import stat
import tempfile
import time
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from enginery.application.behavioral_checks import BehavioralCheck

MAX_BYTES = 1024 * 1024


class BrowserObserverError(Exception):
    """A bounded browser transport or qualification failure, not a product failure."""


def loopback_url(url: str) -> str:
    """Validate HTTP loopback and pin localhost to a numeric address without DNS."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if host == "localhost":
            host = "127.0.0.1"
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port == 0
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url
        ):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ""))
    except ValueError as error:
        raise BrowserObserverError(
            "observer_url_refused: use HTTP(S) loopback without credentials"
        ) from error


def browser_tree_digest(executable: Path) -> str:
    """Hash the distribution using the approved C-locale shasum-list definition.

    The distribution root is executable.parent.parent (the headless-shell
    archive layout). Sort regular-file paths by filesystem bytes, relative to
    the root's parent; hash ``<file sha256>  <relative path>\\n`` lines. Symlinks
    are not followed, matching ``find DIR -type f``. File contents are streamed.
    """
    root = executable.parent.parent
    if (
        executable.is_symlink()
        or executable.parent.is_symlink()
        or not executable.is_file()
        or root.is_symlink()
    ):
        raise BrowserObserverError("browser distribution is missing or linked")
    paths: list[Path] = []
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        subdirectories[:] = [
            name for name in subdirectories if not (Path(directory) / name).is_symlink()
        ]
        for name in files:
            path = Path(directory) / name
            if stat.S_ISREG(path.lstat().st_mode):
                paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: os.fsencode(item.relative_to(root.parent))):
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                file_digest.update(chunk)
        name = os.fsencode(path.relative_to(root.parent))
        # shasum escapes filenames containing backslashes or newlines.
        escaped = b"\\" in name or b"\n" in name
        name = name.replace(b"\\", b"\\\\").replace(b"\n", b"\\n")
        digest.update(
            (b"\\" if escaped else b"")
            + file_digest.hexdigest().encode() + b"  " + name + b"\n"
        )
    return "sha256:" + digest.hexdigest()


def qualify_browser(executable: str | None, expected_digest: str | None) -> Path:
    repair = (
        "repair: restore the approved headless-shell distribution and configure operator "
        "[executables].browser and browser_digest"
    )
    if not executable or not expected_digest:
        raise BrowserObserverError(
            f"browser_observer_unqualified: executable or digest absent; {repair}"
        )
    path = Path(executable).expanduser()
    if not path.is_absolute() or path.name != "chrome-headless-shell":
        raise BrowserObserverError(
            f"browser_observer_unqualified: approved executable required; {repair}"
        )
    try:
        actual = browser_tree_digest(path)
    except (OSError, BrowserObserverError) as error:
        raise BrowserObserverError(
            f"browser_observer_unqualified: distribution unavailable; {repair}"
        ) from error
    if actual != expected_digest or not os.access(path, os.X_OK):
        raise BrowserObserverError(
            f"browser_observer_unqualified: digest mismatch or not executable; {repair}"
        )
    return path


class _Pipe:
    """Small synchronous CDP client; events are serviced while awaiting replies."""

    def __init__(
        self, executable: Path, profile: Path, environment: dict[str, str], deadline: float
    ) -> None:
        self.deadline = deadline
        self.buffer = bytearray()
        self.sequence = 0
        self.loaded: set[str] = set()
        self.session: str | None = None
        self.selector = selectors.DefaultSelector()
        self.pid: int | None = None
        self.write_fd = -1
        self.read_fd = -1
        child_read, parent_write = os.pipe()
        parent_read, child_write = os.pipe()
        # High child sources avoid dup2 collisions with Chromium's mandated fd 3/4.
        read_source = fcntl.fcntl(child_read, fcntl.F_DUPFD_CLOEXEC, 10)
        write_source = fcntl.fcntl(child_write, fcntl.F_DUPFD_CLOEXEC, 10)
        argv = [
            str(executable), "--headless", "--remote-debugging-pipe",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update",
            "--disable-sync", "--disable-default-apps", "--disable-extensions",
            "--disable-domain-reliability", "--disable-breakpad", "--metrics-recording-only",
            "--disable-quic",
            "--disable-features=WebRtcHideLocalIpsWithMdns,MediaRouter,OptimizationHints",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.*, EXCLUDE [::1], "
            "EXCLUDE ::1, EXCLUDE localhost",
            # A dead loopback proxy also blocks literal-IP WebSocket/worker
            # traffic that CDP Fetch does not intercept. Only loopback bypasses.
            "--proxy-server=http://127.0.0.1:9",
            "--proxy-bypass-list=127.0.0.0/8;[::1];localhost",
            "--block-new-web-contents",
            "--window-size=800,600",
            "about:blank",
        ]
        actions = [
            (os.POSIX_SPAWN_DUP2, read_source, 3),
            (os.POSIX_SPAWN_DUP2, write_source, 4),
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_CLOSE, read_source),
            (os.POSIX_SPAWN_CLOSE, write_source),
        ]
        try:
            self.pid = os.posix_spawn(
                str(executable), argv, environment, file_actions=actions, setpgroup=0
            )
            self.write_fd, self.read_fd = parent_write, parent_read
            os.set_blocking(self.read_fd, False)
            os.set_blocking(self.write_fd, False)
            self.selector.register(self.read_fd, selectors.EVENT_READ)
        except BaseException:
            os.close(parent_write)
            os.close(parent_read)
            self.close()
            raise
        finally:
            for descriptor in (child_read, child_write, read_source, write_source):
                os.close(descriptor)

    def close(self) -> None:
        if self.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)
            self.pid = None
        self.selector.close()
        for descriptor in (self.write_fd, self.read_fd):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        self.write_fd = self.read_fd = -1

    def _send(self, method: str, params: dict[str, object], session: str | None) -> int:
        self.sequence += 1
        packet: dict[str, object] = {"id": self.sequence, "method": method, "params": params}
        if session is not None:
            packet["sessionId"] = session
        raw = json.dumps(packet, ensure_ascii=True).encode() + b"\x00"
        if len(raw) > MAX_BYTES:
            raise BrowserObserverError("browser_output_limit: CDP request exceeds 1 MiB")
        offset = 0
        while offset < len(raw):
            if time.monotonic() >= self.deadline:
                raise BrowserObserverError("observer_timeout: browser pipe write")
            try:
                offset += os.write(self.write_fd, raw[offset:])
            except BlockingIOError:
                time.sleep(0.005)
        return self.sequence

    def _receive(self) -> dict[str, object]:
        while b"\x00" not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise BrowserObserverError("observer_timeout: browser pipe response")
            chunk = os.read(self.read_fd, 65536)
            if not chunk:
                raise BrowserObserverError("browser_transport_failed: CDP pipe closed")
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_BYTES:
                raise BrowserObserverError("browser_output_limit: CDP response exceeds 1 MiB")
        raw, _, remainder = self.buffer.partition(b"\x00")
        self.buffer = bytearray(remainder)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise BrowserObserverError("browser_transport_failed: malformed CDP response")
        return cast(dict[str, object], value)

    def _event(self, packet: dict[str, object]) -> None:
        method = packet.get("method")
        if method == "Page.lifecycleEvent" and packet.get("sessionId") == self.session:
            lifecycle = packet.get("params")
            if isinstance(lifecycle, dict) and lifecycle.get("name") == "load":
                loader = lifecycle.get("loaderId")
                if isinstance(loader, str):
                    self.loaded.add(loader)
                    if len(self.loaded) > 64:
                        raise BrowserObserverError(
                            "browser_output_limit: too many page navigation events"
                        )
        if method == "Fetch.requestPaused":
            params = packet.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("request"), dict):
                raise BrowserObserverError("browser_transport_failed: malformed Fetch event")
            request = params["request"]
            url = request.get("url")
            normalized: str | None = None
            if isinstance(url, str):
                with contextlib.suppress(BrowserObserverError):
                    normalized = loopback_url(url)
            target = packet.get("sessionId")
            request_id = params.get("requestId")
            if not isinstance(target, str) or not isinstance(request_id, str):
                raise BrowserObserverError("browser_transport_failed: malformed Fetch identity")
            allowed = normalized is not None
            parameters: dict[str, object] = {"requestId": request_id}
            if allowed:
                parameters["url"] = normalized
            else:
                parameters["errorReason"] = "BlockedByClient"
            self._send(
                "Fetch.continueRequest" if allowed else "Fetch.failRequest",
                parameters,
                target,
            )

    def call(
        self, method: str, params: dict[str, object] | None = None, *, root: bool = False
    ) -> dict[str, object]:
        identifier = self._send(method, params or {}, None if root else self.session)
        while True:
            packet = self._receive()
            if packet.get("id") == identifier:
                if "error" in packet:
                    raise BrowserObserverError("browser_transport_failed: CDP command rejected")
                result = packet.get("result")
                if not isinstance(result, dict):
                    raise BrowserObserverError("browser_transport_failed: missing CDP result")
                return cast(dict[str, object], result)
            if "id" not in packet:
                self._event(packet)
            # Event acknowledgement responses have no consumer and can be discarded.

    def wait_loaded(self, loader: str) -> None:
        while loader not in self.loaded:
            packet = self._receive()
            if "id" not in packet:
                self._event(packet)


def observe_browser(
    check: BehavioralCheck, *, executable: Path, environment: dict[str, str], deadline: float
) -> tuple[tuple[tuple[int, bool], ...], bytes]:
    """Return evaluated DOM comparisons and original PNG bytes, not a verdict."""
    url = loopback_url(check.url or "")
    with tempfile.TemporaryDirectory(prefix="enginery-browser-") as temporary:
        profile = Path(temporary)
        profile.chmod(0o700)
        pipe = _Pipe(executable, profile, environment, deadline)
        try:
            target = pipe.call("Target.createTarget", {"url": "about:blank"}, root=True)
            attached = pipe.call(
                "Target.attachToTarget",
                {"targetId": target["targetId"], "flatten": True},
                root=True,
            )
            session = attached.get("sessionId")
            if not isinstance(session, str):
                raise BrowserObserverError("browser_transport_failed: missing target session")
            pipe.session = session
            pipe.call("Page.enable")
            pipe.call("Page.setLifecycleEventsEnabled", {"enabled": True})
            pipe.call("Network.enable")
            pipe.call("Network.setBypassServiceWorker", {"bypass": True})
            pipe.call("Network.setCacheDisabled", {"cacheDisabled": True})
            pipe.call(
                "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
            )
            navigation = pipe.call("Page.navigate", {"url": url})
            if navigation.get("errorText"):
                error_text = str(navigation["errorText"])
                code = error_text if error_text.startswith("net::ERR_") else "unknown"
                raise BrowserObserverError(f"browser_navigation_failed: {code[:96]}")
            loader = navigation.get("loaderId")
            if not isinstance(loader, str):
                raise BrowserObserverError("browser_navigation_failed: missing navigation identity")
            pipe.wait_loaded(loader)
            results: list[tuple[int, bool]] = []
            for index, assertion in enumerate(check.assertions):
                if assertion.kind != "dom":
                    continue
                selector = json.dumps(assertion.selector)
                expected = json.dumps(assertion.expected)
                if assertion.operator == "exists":
                    comparison = f"(element !== null) === {expected}"
                elif assertion.operator == "equals":
                    comparison = f"element !== null && element.textContent === {expected}"
                else:
                    comparison = f"element !== null && element.textContent.includes({expected})"
                expression = (
                    f"(() => {{ const element = document.querySelector({selector}); "
                    f"return {comparison}; }})()"
                )
                observed = pipe.call(
                    "Runtime.evaluate", {"expression": expression, "returnByValue": True}
                )
                result = observed.get("result")
                if (
                    "exceptionDetails" in observed
                    or not isinstance(result, dict)
                    or not isinstance(result.get("value"), bool)
                ):
                    raise BrowserObserverError(
                        "browser_dom_unobserved: invalid selector or evaluation failure"
                    )
                results.append((index, result["value"]))
            screenshot = pipe.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            encoded = screenshot.get("data")
            if not isinstance(encoded, str):
                raise BrowserObserverError("browser_capture_failed: missing PNG")
            png = base64.b64decode(encoded, validate=True)
            if len(png) > MAX_BYTES:
                raise BrowserObserverError("browser_output_limit: PNG exceeds 1 MiB")
            return tuple(results), png
        finally:
            pipe.close()
