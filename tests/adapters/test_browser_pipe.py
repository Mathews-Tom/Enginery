"""Browser policy regressions without launching an approved browser."""

from pathlib import Path

import pytest

from enginery.adapters import browser_pipe
from enginery.application.behavioral_checks import BehavioralAssertion, BehavioralCheck


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://127.0.0.1:8080/assets/app.js", True),
        ("http://127.0.0.1:8080/other?x=1", True),
        ("https://127.0.0.1:8080/", False),
        ("http://127.0.0.1:8081/", False),
        ("http://127.0.0.1/", False),
        ("http://127.0.0.2:8080/", False),
        ("http://localhost:8080/", False),
        ("http://[::1]:8080/", False),
        ("https://example.com/", False),
        ("file:///etc/passwd", False),
        ("http://127.0.0.1:bad/", False),
    ],
)
def test_fetch_requires_exact_declared_origin(url: str, allowed: bool) -> None:
    sent: list[tuple[str, dict[str, object], str | None]] = []

    class PolicyPipe(browser_pipe._Pipe):
        def __init__(self) -> None:
            self.origin = browser_pipe._origin("http://127.0.0.1:8080/")
            self.session = "page"

        def _send(self, method: str, params: dict[str, object], session: str | None) -> int:
            sent.append((method, params, session))
            return 1

    PolicyPipe()._event(
        {
            "method": "Fetch.requestPaused",
            "sessionId": "page",
            "params": {"requestId": "request", "request": {"url": url}},
        }
    )
    method, parameters, session = sent[0]
    assert method == ("Fetch.continueRequest" if allowed else "Fetch.failRequest")
    assert session == "page"
    assert parameters["requestId"] == "request"
    if not allowed:
        assert parameters["errorReason"] == "BlockedByClient"
        assert "url" not in parameters


def test_default_port_is_same_origin() -> None:
    assert browser_pipe._origin("http://127.0.0.1/") == browser_pipe._origin(
        "http://127.0.0.1:80/path"
    )
    assert browser_pipe._origin("https://[::1]/") == browser_pipe._origin("https://[::1]:443/path")


def test_dom_evaluation_uses_isolated_world(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    closed: list[bool] = []

    class FakePipe:
        def __init__(
            self,
            executable: Path,
            profile: Path,
            environment: dict[str, str],
            deadline: float,
            origin_url: str,
        ) -> None:
            self.session: str | None = None
            assert origin_url == "http://127.0.0.1:8080/"

        def call(
            self, method: str, params: dict[str, object] | None = None, *, root: bool = False
        ) -> dict[str, object]:
            calls.append((method, params or {}))
            responses: dict[str, dict[str, object]] = {
                "Target.createTarget": {"targetId": "target"},
                "Target.attachToTarget": {"sessionId": "page"},
                "Page.navigate": {"loaderId": "loader", "frameId": "main-frame"},
                "Page.createIsolatedWorld": {"executionContextId": 42},
                "Runtime.evaluate": {"result": {"value": True}},
                "Page.captureScreenshot": {"data": "cG5n"},
            }
            return responses.get(method, {})

        def wait_loaded(self, loader: str) -> None:
            assert loader == "loader"

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(browser_pipe, "_Pipe", FakePipe)
    check = BehavioralCheck(
        name="page",
        observer="browser",
        assertions=(BehavioralAssertion("dom", "equals", "5", selector="#result"),),
        fault_classes=("constant_result", "wrong_build"),
        url="http://127.0.0.1:8080/",
    )
    result = browser_pipe.observe_browser(
        check, executable=Path("unused-browser"), environment={}, deadline=1.0
    )
    assert result == (((0, True),), b"png")
    methods = [method for method, _ in calls]
    assert methods.index("Page.createIsolatedWorld") < methods.index("Runtime.evaluate")
    world = next(params for method, params in calls if method == "Page.createIsolatedWorld")
    assert world == {"frameId": "main-frame", "worldName": "enginery-observer"}
    evaluation = next(params for method, params in calls if method == "Runtime.evaluate")
    assert evaluation["contextId"] == 42
    assert evaluation["returnByValue"] is True
    assert closed == [True]
