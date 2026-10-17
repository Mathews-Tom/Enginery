"""Presentation is local, bounded and never an authority channel."""

from __future__ import annotations

import html
from pathlib import Path

import pytest

from enginery.adapters import review_presenters as presenters
from enginery.adapters import subprocess_output
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
        return '{"prompts":[{"prompt":"repair this","text":"packet"}],"decision":"approved"}'

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
    assert presenters._findings('{"prompts":[{"prompt":"fix\\u001b[31m this\\u202e"}]}') == (
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tabular_one", ("Rename the flag",)),
        ("tabular_tricky", ('Use "a, b": c\n\nContext data:\n{"q": 1}\x1b[31m', "- second, item")),
        ("expanded_target", ("Fix the table", "plain")),
        ("attachments", ("see image",)),
        ("waiting", ()),
        ("ended", ()),
        ("annotation_tabular", ("Rename the flag before merge",)),
        ("two_prompts", ("Rename the flag before merge", "Add a regression test")),
        ("with_target_expanded", ("Rename the flag before merge",)),
        ("nel_text_two_prompts", ("First comment", "Second comment")),
        ("nel_text_quoted", ("Only comment",)),
        ("nel_snapshot", ("Rename the flag",)),
        ("ls_prompt", ("line one\u2028line two", "next comment")),
    ],
)
def test_encoder_produced_poll_prompts(name: str, expected: tuple[str, ...]) -> None:
    fixtures = Path(__file__).parents[1] / "fixtures" / "lavish"
    cases = {
        name: body
        for filename in ("encode_poll.out", "encode_poll2.out", "encode_nel.out")
        for section in (fixtures / filename).read_text(encoding="utf-8").split("=====")[1:]
        for name, body in [section.split("\n", 1)]
    }
    assert presenters._findings(cases[name]) == tuple(map(presenters.escape_terminal, expected))


def test_packet_text_is_never_feedback() -> None:
    assert presenters._findings('{"prompts":[{"text":"packet text"}]}') == ()
    assert presenters._findings("prompts[1]:\n  - text: packet text\n") == ()
    with pytest.raises(PresenterError, match="lack prompt"):
        presenters._findings("prompts[1]{uid,text}:\n  e1,packet text\n")


def test_encoder_artifact_failure_is_refused() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "lavish" / "encode_poll2.out"
    cases = dict(
        section.split("\n", 1) for section in fixture.read_text(encoding="utf-8").split("=====")[1:]
    )
    with pytest.raises(PresenterError, match="artifact failures"):
        presenters._findings(cases["artifact_failure"])


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

    monkeypatch.setattr(subprocess_output.subprocess, "Popen", forbidden)
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

    monkeypatch.setattr(subprocess_output.subprocess, "Popen", missing)
    with pytest.raises(PresenterError, match="could not be started") as failure:
        presenters._execute(("missing", "poll"), {})
    assert "sensitive" not in str(failure.value)
