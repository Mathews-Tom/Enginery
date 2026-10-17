"""Real processes and loopback HTTP; the approved browser is explicit opt-in."""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from enginery.adapters.behavioral_observers import observe
from enginery.adapters.browser_pipe import browser_tree_digest
from enginery.application.behavioral_checks import BehavioralCheck
from enginery.domain.behavioral_evidence import DecodeStatus
from enginery.evidence.media import decode_png


def _check(
    observer: str,
    *,
    argv: tuple[str, ...] = (),
    url: str | None = None,
    assertions: list[dict[str, object]] | None = None,
) -> BehavioralCheck:
    raw: dict[str, object] = {
        "name": "runtime",
        "observer": observer,
        "assertions": (
            assertions if assertions is not None else [{"kind": "exit_status", "equals": 0}]
        ),
        "fault_classes": ["constant_result"],
    }
    if argv:
        raw["argv"] = list(argv)
    if url is not None:
        raw["url"] = url
    return BehavioralCheck.from_state(raw)


@contextmanager
def _server(
    *,
    body: bytes = b"changed behavior",
    status: int = 200,
    location: str | None = None,
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if location:
                self.send_header("Location", location)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_command_observes_actual_exit_streams_files_and_literal_argv(tmp_path: Path) -> None:
    literal = "$(touch should-not-exist); echo not-a-shell"
    code = (
        "import pathlib,sys; pathlib.Path('result.txt').write_text(sys.argv[1]); "
        "print('changed'); print('diagnostic', file=sys.stderr); sys.exit(7)"
    )
    check = _check(
        "command",
        argv=(sys.executable, "-c", code, literal),
        assertions=[
            {"kind": "exit_status", "equals": 7},
            {"kind": "produced_file", "path": "result.txt", "equals": literal},
        ],
    )
    data = observe(check, cwd=tmp_path)
    assert data.infrastructure_error is None
    assert data.returncode == 7
    assert data.stdout == "changed\n"
    assert data.stderr == "diagnostic\n"
    assert data.produced_files == (("result.txt", literal),)
    assert data.started_at <= data.ended_at
    assert data.ran and not data.mock and not data.skipped
    assert not (tmp_path / "should-not-exist").exists()


def test_command_does_not_inherit_credentials_or_python_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "must-not-be-inherited")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "injected"))
    code = "import os; print('GH_TOKEN' in os.environ, 'PYTHONPATH' in os.environ)"
    data = observe(_check("command", argv=(sys.executable, "-c", code)), cwd=tmp_path)
    assert data.infrastructure_error is None
    assert data.stdout == "False False\n"


def test_missing_executable_is_infrastructure_not_behavior_failure(tmp_path: Path) -> None:
    data = observe(_check("command", argv=(str(tmp_path / "missing-tool"),)), cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_tool_missing:")
    assert data.returncode is None and not data.ran


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_command_output_is_bounded(tmp_path: Path, stream: str) -> None:
    code = f"import sys; sys.{stream}.write('x' * (2 * 1024 * 1024)); sys.{stream}.flush()"
    data = observe(_check("command", argv=(sys.executable, "-c", code)), cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_output_limit:")
    assert len(data.stdout.encode()) + len(data.stderr.encode()) <= 1024 * 1024


def test_output_limit_survives_exited_group_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS raises EPERM when every member of the killed group exited but is unreaped."""
    real_killpg = os.killpg

    def killpg_then_eperm(pid: int, signal_number: int) -> None:
        with suppress(ProcessLookupError, PermissionError):
            real_killpg(pid, signal_number)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", killpg_then_eperm)
    code = "import sys; sys.stdout.write('x' * (2 * 1024 * 1024)); sys.stdout.flush()"
    data = observe(_check("command", argv=(sys.executable, "-c", code)), cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_output_limit:")


def test_command_timeout_is_inconclusive_and_stops_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("enginery.adapters.behavioral_observers.OBSERVATION_TIMEOUT_SECONDS", 0.2)
    code = (
        "import pathlib,time; pathlib.Path('started').write_text('yes'); "
        "time.sleep(1); pathlib.Path('escaped').write_text('no')"
    )
    data = observe(_check("command", argv=(sys.executable, "-c", code)), cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_timeout:")
    assert data.ran
    time.sleep(1)
    assert not (tmp_path / "escaped").exists()


def test_background_descendant_is_stopped_when_command_exits(tmp_path: Path) -> None:
    child = "import pathlib,time; time.sleep(0.5); pathlib.Path('escaped').write_text('no')"
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1]])"
    data = observe(_check("command", argv=(sys.executable, "-c", code, child)), cwd=tmp_path)
    assert data.infrastructure_error is None
    time.sleep(0.7)
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("link_kind", ["file", "directory", "hardlink"])
def test_produced_files_refuse_links_outside_checkout(tmp_path: Path, link_kind: str) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("not evidence")
    name = "result.txt"
    if link_kind == "directory":
        (candidate / "escape").symlink_to(tmp_path, target_is_directory=True)
        name = "escape/secret.txt"
    elif link_kind == "hardlink":
        os.link(secret, candidate / name)
    else:
        (candidate / name).symlink_to(secret)
    data = observe(
        _check(
            "command",
            argv=(sys.executable, "-c", "pass"),
            assertions=[
                {"kind": "produced_file", "path": name, "contains": "evidence"},
            ],
        ),
        cwd=candidate,
    )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_produced_file_unsafe:")
    assert data.produced_files == ()


def test_missing_produced_file_is_observed_absence_not_infrastructure(tmp_path: Path) -> None:
    data = observe(
        _check(
            "command",
            argv=(sys.executable, "-c", "pass"),
            assertions=[
                {"kind": "produced_file", "path": "missing", "exists": False},
            ],
        ),
        cwd=tmp_path,
    )
    assert data.infrastructure_error is None
    assert data.produced_files == ()


def test_produced_file_output_is_bounded(tmp_path: Path) -> None:
    code = "from pathlib import Path; Path('large').write_bytes(b'x' * (1024 * 1024 + 1))"
    data = observe(
        _check(
            "command",
            argv=(sys.executable, "-c", code),
            assertions=[
                {"kind": "produced_file", "path": "large", "contains": "x"},
            ],
        ),
        cwd=tmp_path,
    )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_output_limit:")


def test_service_observes_real_body_without_ambient_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    with _server(body=b"real changed path", status=418) as url:
        data = observe(
            _check(
                "service",
                url=url,
                assertions=[{"kind": "body", "equals": "real changed path"}],
            ),
            cwd=tmp_path,
        )
    assert data.infrastructure_error is None
    assert data.body == "real changed path"
    assert data.returncode == 418


def test_service_starts_and_tears_down_declared_server(tmp_path: Path) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    (tmp_path / "index.html").write_text("served from candidate")
    url = f"http://127.0.0.1:{port}/"
    check = _check(
        "service",
        argv=(sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"),
        url=url,
        assertions=[{"kind": "body", "contains": "candidate"}],
    )
    data = observe(check, cwd=tmp_path)
    assert data.infrastructure_error is None
    assert data.body == "served from candidate"
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_service_redirect_refused_even_to_loopback(tmp_path: Path) -> None:
    with _server(status=302, location="http://127.0.0.1:1/should-not-connect") as url:
        data = observe(
            _check("service", url=url, assertions=[{"kind": "body", "contains": "x"}]),
            cwd=tmp_path,
        )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_redirect_refused:")


def test_service_body_bounded(tmp_path: Path) -> None:
    with _server(body=b"x" * (1024 * 1024 + 1)) as url:
        data = observe(
            _check("service", url=url, assertions=[{"kind": "body", "contains": "x"}]),
            cwd=tmp_path,
        )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_output_limit:")


def test_unavailable_service_is_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("enginery.adapters.behavioral_observers.OBSERVATION_TIMEOUT_SECONDS", 0.15)
    data = observe(
        _check(
            "service",
            url="http://127.0.0.1:1/",
            assertions=[{"kind": "body", "contains": "x"}],
        ),
        cwd=tmp_path,
    )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_timeout:")
    assert not data.ran


def test_nonloopback_observer_refused_before_start(tmp_path: Path) -> None:
    check = BehavioralCheck(
        name="unsafe",
        observer="service",
        assertions=(),
        fault_classes=("constant_result",),
        url="http://192.0.2.1/",
    )
    data = observe(check, cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_url_refused:")
    assert not data.ran


def test_browser_absent_is_exact_repair_hold(tmp_path: Path) -> None:
    check = _check(
        "browser",
        url="http://127.0.0.1:1/",
        assertions=[{"kind": "dom", "selector": "h1", "equals": "changed"}],
    )
    data = observe(check, cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("browser_observer_unqualified:")
    assert "repair:" in data.infrastructure_error
    assert not data.ran and not data.captures and not data.dom_results


def test_browser_tree_digest_matches_c_locale_and_holds_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "chromium_headless_shell-1234"
    executable = root / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"not an executable browser")
    executable.chmod(0o700)
    (root / "Z").write_bytes(b"Z")
    (root / "a").write_bytes(b"a")
    lines = b"".join(
        hashlib.sha256(path.read_bytes()).hexdigest().encode()
        + b"  "
        + os.fsencode(path.relative_to(root.parent))
        + b"\n"
        for path in (root / "Z", root / "a", executable)
    )
    expected = "sha256:" + hashlib.sha256(lines).hexdigest()
    assert browser_tree_digest(executable) == expected
    check = _check(
        "browser",
        url="http://127.0.0.1:1/",
        assertions=[{"kind": "dom", "selector": "h1", "exists": True}],
    )
    data = observe(
        check,
        cwd=tmp_path,
        browser_executable=str(executable),
        browser_expected_digest="sha256:" + "0" * 64,
    )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("browser_observer_unqualified:")
    assert "digest mismatch" in data.infrastructure_error and "repair:" in data.infrastructure_error
    assert not data.ran


def test_approved_browser_observes_dom_and_captures_png(tmp_path: Path) -> None:
    executable = os.environ.get("ENGINERY_BROWSER_EXECUTABLE")
    expected = os.environ.get("ENGINERY_BROWSER_DIGEST")
    if not executable or not expected or not Path(executable).is_file():
        pytest.skip(
            "opt in with ENGINERY_BROWSER_EXECUTABLE and ENGINERY_BROWSER_DIGEST "
            "for the approved shell"
        )
    body = b"<!doctype html><h1 id='changed'>real browser behavior</h1>"
    with _server(body=body) as url:
        check = _check(
            "browser",
            url=url,
            assertions=[
                {"kind": "dom", "selector": "#changed", "equals": "real browser behavior"},
                {"kind": "dom", "selector": "#absent", "exists": False},
                {"kind": "dom", "selector": "#changed", "contains": "wrong expected text"},
            ],
        )
        data = observe(
            check,
            cwd=tmp_path,
            browser_executable=executable,
            browser_expected_digest=expected,
        )
    assert data.infrastructure_error is None
    assert data.dom_results == ((0, True), (1, True), (2, False))
    assert len(data.captures) == 1
    media_type, png = data.captures[0]
    assert media_type == "image/png" and png.startswith(b"\x89PNG\r\n\x1a\n")
    assert decode_png(png).status is DecodeStatus.DECODABLE
    assert not data.mock and data.ran


def test_service_does_not_observe_a_preexisting_server_as_its_new_build(tmp_path: Path) -> None:
    code = "from pathlib import Path; Path('unexpected-start').write_text('started')"
    with _server(body=b"stale build") as url:
        data = observe(
            _check(
                "service",
                argv=(sys.executable, "-c", code),
                url=url,
                assertions=[{"kind": "body", "equals": "stale build"}],
            ),
            cwd=tmp_path,
        )
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_service_conflict:")
    assert not data.ran and not (tmp_path / "unexpected-start").exists()


def test_undecodable_output_is_infrastructure_not_replacement_text(tmp_path: Path) -> None:
    check = _check("command", argv=(sys.executable, "-c", "import os; os.write(1, bytes([255]))"))
    data = observe(check, cwd=tmp_path)
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("observer_output_undecodable:")
    assert data.stdout == ""
