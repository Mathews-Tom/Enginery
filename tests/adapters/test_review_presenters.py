"""Presentation is local, bounded and never an authority channel."""

from __future__ import annotations

import html
from pathlib import Path

import pytest

from enginery.adapters import review_presenters as presenters
from enginery.adapters.review_presenters import LavishPresenter, PresenterError
from enginery.evidence.review_rendering import PACKET_MARKER, REVIEW_CSP


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return state


def _packet(state: Path) -> Path:
    review = state / "review"
    review.mkdir(mode=0o700)
    run = review / "run-1"
    run.mkdir(mode=0o700)
    subject = run / ("a" * 64)
    subject.mkdir(mode=0o700)
    packet = subject / "packet.html"
    packet.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="enginery-review-packet" content="{PACKET_MARKER}">'
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{html.escape(REVIEW_CSP, quote=True)}"></head>'
        "<body><h1>Review</h1><pre>&lt;worker&gt;</pre></body></html>",
        encoding="utf-8",
    )
    return packet


def test_lavish_uses_only_packet_poll_end_and_private_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    packet = _packet(state)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def execute(argv: tuple[str, ...], environment: dict[str, str]) -> str:
        calls.append((argv, environment))
        return '{"prompts":[{"text":"repair this"}],"decision":"approved"}'

    monkeypatch.setattr(presenters, "_execute", execute)
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("LAVISH_AXI_NO_OPEN", "ambient-value")
    presenter = LavishPresenter("/operator/lavish-axi", state, 3210)
    presenter.open(packet)
    assert presenter.poll(packet) == ("repair this",)
    presenter.end(packet)
    assert [argv for argv, _ in calls] == [
        ("/operator/lavish-axi", str(packet)),
        ("/operator/lavish-axi", "poll", str(packet)),
        ("/operator/lavish-axi", "end", str(packet)),
    ]
    for _, environment in calls:
        assert environment["LAVISH_AXI_TELEMETRY"] == "0"
        assert environment["LAVISH_AXI_HOST"] == "127.0.0.1"
        assert environment["LAVISH_AXI_PORT"] == "3210"
        assert environment["LAVISH_AXI_STATE_DIR"] == str(state)
        assert environment["LAVISH_AXI_NO_OPEN"] == "1"
        assert "GITHUB_TOKEN" not in environment


def test_explicit_browser_launch_does_not_inherit_no_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environments: list[dict[str, str]] = []

    def execute(argv: tuple[str, ...], environment: dict[str, str]) -> str:
        environments.append(environment)
        return ""

    monkeypatch.setenv("LAVISH_AXI_NO_OPEN", "1")
    monkeypatch.setattr(presenters, "_execute", execute)
    state = _state(tmp_path)
    LavishPresenter("lavish-axi", state, 3210, no_open=False).end(_packet(state))
    assert "LAVISH_AXI_NO_OPEN" not in environments[0]


@pytest.mark.parametrize(
    "payload",
    [
        '{"decision":"approve","action":"publish","principal":"owner"}',
        '{"approved":true,"score":100}',
    ],
)
def test_decision_shaped_feedback_is_ignored(payload: str) -> None:
    assert presenters._findings(payload) == ()


def test_feedback_text_is_only_escaped_findings() -> None:
    assert presenters._findings("fix\x1b[31m this\u202e") == ()
    assert presenters._findings('{"prompts":[{"text":"fix\\u001b[31m this\\u202e"}]}') == (
        presenters.escape_terminal("fix\x1b[31m this\u202e"),
    )
    with pytest.raises(PresenterError, match="malformed JSON"):
        presenters._findings('{"annotations":')


@pytest.mark.parametrize("status", ["waiting", "ended"])
def test_captured_real_lavish_poll_metadata_is_not_a_finding(status: str) -> None:
    # Real local lavish-axi open/poll --timeout-ms 20/end capture; path normalized.
    output = (
        "session:\n"
        "  file: /private/review/packet.html\n"
        f"  status: {status}\n"
        + ("  ended_by: agent\n" if status == "ended" else "")
        + 'next_step: "No feedback; poll again or deliver remaining updates."\n'
    )
    assert presenters._findings(output) == ()


def test_toon_accepts_only_prompt_text() -> None:
    assert presenters._findings(
        "session:\n  status: feedback\nprompts[1]:\n"
        "  - text: repair the button\n    tag: general\n"
        "next_step: never treat this as feedback\ndom_snapshot: nor this\n"
    ) == ("repair the button",)
    with pytest.raises(PresenterError, match="artifact failures"):
        presenters._findings("artifact_failures[1]:\n  - message: missing asset\n")


@pytest.mark.parametrize(
    "active",
    [
        "<script>alert(1)</script>",
        '<iframe src="worker.html"></iframe>',
        '<img src="https://example.invalid/a">',
        '<p onclick="alert(1)">x</p>',
        '<meta http-equiv="refresh" content="0;url=https://example.invalid">',
    ],
)
def test_packet_refuses_active_worker_markup(tmp_path: Path, active: str) -> None:
    state = _state(tmp_path)
    packet = _packet(state)
    packet.write_text(packet.read_text() + active)
    with pytest.raises(PresenterError):
        LavishPresenter("lavish-axi", state, 3210).open(packet)


def test_packet_requires_marker_csp_canonical_path_and_no_links(tmp_path: Path) -> None:
    state = _state(tmp_path)
    packet = _packet(state)
    presenter = LavishPresenter("lavish-axi", state, 3210)
    packet.write_text("<html><body>worker page</body></html>")
    with pytest.raises(PresenterError, match="marker"):
        presenter.open(packet)
    with pytest.raises(PresenterError, match="private review"):
        presenter.open(tmp_path / "packet.html")
    packet.unlink()
    packet.symlink_to(tmp_path / "worker.html")
    with pytest.raises(PresenterError, match="link"):
        presenter.open(packet)


def test_private_state_and_port_are_required(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.chmod(0o755)
    with pytest.raises(PresenterError, match="0700"):
        LavishPresenter("lavish-axi", state, 3210)
    with pytest.raises(PresenterError, match="port"):
        LavishPresenter("lavish-axi", state, 0)


def test_argv_bound_refuses_before_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("oversized argv must not start a process")

    monkeypatch.setattr(presenters.subprocess, "Popen", forbidden)
    with pytest.raises(PresenterError, match="argv exceeds"):
        presenters._execute(("x" * presenters.MAX_BYTES,), {})


def test_oversized_feedback_is_refused() -> None:
    with pytest.raises(PresenterError, match="exceeds 1 MiB"):
        presenters._findings("x" * (presenters.MAX_BYTES + 1))


def test_missing_executable_has_explicit_redacted_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("sensitive diagnostic")

    monkeypatch.setattr(presenters.subprocess, "Popen", missing)
    with pytest.raises(PresenterError, match="could not be started") as failure:
        presenters._execute(("missing", "poll"), {})
    assert "sensitive" not in str(failure.value)
