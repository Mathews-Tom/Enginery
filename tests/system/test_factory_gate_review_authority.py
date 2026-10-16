"""Authority gate dispatch, actual CLI journey and fail-closed evidence coverage."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import factory_gate_review_authority as authority
from factory_gate_evidence import EXIT_FAIL, EXIT_PASS, validate_evidence
from factory_gate_manual_path import TerminalSession
from run_factory_gate import default_scenarios, main, parse_options


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["--scenario", "review-authority", "--evidence-out", str(tmp_path / "evidence.json"),
            *extra]


def _document(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8")))


def test_review_authority_exercises_real_cli_and_every_required_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # The unconfigured terminal path is mandatory and never needs an optional host presenter.
    monkeypatch.setattr(authority, "operator_lavish", lambda: None)
    code = main(_argv(tmp_path))
    document = _document(tmp_path)

    assert code == EXIT_PASS, document.get("details")
    assert validate_evidence(document) == []
    assert document["mode"] == "local" and document["verdict"] == "pass"
    assertions = {item["id"]: item for item in document["assertions"]}
    assert set(assertions) == set(authority.REQUIRED)
    assert all(item["ran"] and item["status"] == "pass" for item in assertions.values())
    assert all(item["commands"] and item["observer"] and item["raw_artifacts"]
               for item in assertions.values())
    fresh = assertions["fresh-decision-once"]["observed"]["decisions"]
    assert len(fresh) == 1
    assert fresh[0]["principal_id"] == authority.OPERATOR_PRINCIPAL
    assert fresh[0]["action"] == "pull_request.open"
    assert fresh[0]["os_uid"] == os.getuid()
    superseded = assertions["changed-evidence-supersedes"]["observed"]["decisions"]
    assert superseded[0]["decision_id"] == fresh[0]["decision_id"]
    assert superseded[0]["superseded_by"]
    assert assertions["terminal-feedback-no-authority"]["observed"]["decision_count"] == 0
    http = assertions["http-no-authority"]["observed"]
    assert http["status"] == 403
    assert "decision_authentication_required" in http["cli"][0]["text"]
    history = assertions["challenge-secrecy-history"]["observed"]
    assert history["plaintext_leak_paths"] == []
    assert len(history["decisions"]) == 2
    assert (tmp_path / "evidence.raw" / history["retained_ledger"]).is_file()
    effects = document["external_effect_counts"]
    assert effects["pull_request_creations"] == 1
    assert effects["review_requests"] == effects["hosted_sessions"] == 0
    assert effects["container_engine_calls"] == effects["hosted_inference_calls"] == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"


def test_review_authority_dispatch_is_local_and_has_no_github_pagination_assertions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority, "operator_lavish", lambda: None)
    options = parse_options(_argv(tmp_path))
    spec = default_scenarios()["review-authority"]

    assert spec.prepare is authority.prepare and not spec.need_relay
    assert spec.required(options) == authority.REQUIRED
    assert not any(key.startswith("github-") for key in spec.required(options))
    monkeypatch.setattr(authority, "operator_lavish", lambda: "/operator/lavish-axi")
    assert set(spec.required(options)) == set(authority.REQUIRED) | set(authority.LAVISH_REQUIRED)


def test_unrun_authority_assertions_never_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority, "operator_lavish", lambda: None)
    monkeypatch.setattr(authority, "run", lambda gate: None)
    code = main(_argv(tmp_path))
    document = _document(tmp_path)

    assert code == EXIT_FAIL and document["verdict"] == "fail"
    assert {item["id"] for item in document["assertions"]} == set(authority.REQUIRED)
    assert all(not item["ran"] and item["status"] == "fail" for item in document["assertions"])


def test_failed_authority_prerequisite_does_not_pass_unreachable_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fixture(gate: authority.ReviewAuthorityGate) -> None:
        raise RuntimeError("candidate fixture deliberately unavailable")

    monkeypatch.setattr(authority, "operator_lavish", lambda: None)
    monkeypatch.setattr(authority, "_fixture", fail_fixture)
    code = main(_argv(tmp_path))
    assertions = {item["id"]: item for item in _document(tmp_path)["assertions"]}

    assert code == EXIT_FAIL
    for key in ("fresh-decision-once", "authorized-publication", "challenge-secrecy-history"):
        assert assertions[key]["ran"] is False
        assert assertions[key]["status"] == "fail"
    assert assertions["no-review-triggers"]["status"] == "fail"


def test_terminal_session_uses_same_real_tty_and_scrubs_the_only_secret(tmp_path: Path) -> None:
    terminal = TerminalSession()
    secret = "a" * 64
    subject = "sha256:" + "b" * 64
    code = (
        "import os,sys; "
        "assert sys.stdin.isatty() and sys.stdout.isatty(); "
        "assert os.ttyname(0) == os.ttyname(1); "
        f"print('Subject digest: {subject}'); print('Decision challenge: {secret}')"
    )
    try:
        result = terminal.run([sys.executable, "-c", code], cwd=tmp_path, env=os.environ)
        assert result.code == 0 and result.challenge == secret and result.subject == subject
        assert secret not in result.text and secret not in repr(result)
        marker = "<challenge-sha256:" + hashlib.sha256(secret.encode()).hexdigest() + ">"
        assert result.text.count(marker) == 1
        assert terminal.scrub("--challenge " + secret) == "--challenge " + marker
    finally:
        terminal.close()
        terminal.close()


@pytest.mark.parametrize("extra", [
    ["--live"], ["--harness", "omp"], ["--harness", "claude"], ["--model", "example/model"],
    ["--confirm-single-session"], ["--custody-receipt", "receipt.json"],
    ["--gateway-url", "http://127.0.0.1:1"], ["--gateway-token-file", "token"],
    ["--docker-context", "fixture"], ["--runner-image", "sha256:" + "a" * 64],
    ["--relay-image", "sha256:" + "a" * 64], ["--session-deadline-seconds", "120"],
    ["--session-deadline-seconds=120"], ["--session-dead=120"],
])
def test_review_authority_rejects_hosted_engine_and_custody_flags(
    tmp_path: Path, extra: list[str],
) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_options(_argv(tmp_path, *extra))

    assert refused.value.code == 2
    assert not (tmp_path / "evidence.json").exists()
