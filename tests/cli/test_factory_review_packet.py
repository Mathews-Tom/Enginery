from __future__ import annotations

import html
import json
import subprocess
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from enginery.application.review_packet import MAX_PACKET_BYTES, ReviewPacket
from enginery.cli import factory_review
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.evidence.review_rendering import (
    PACKET_MARKER,
    REVIEW_CSP,
    escape_terminal,
    render_html,
    render_terminal,
)
from enginery.workflows.review import ReviewOutcome, ReviewReport, route_review


def _packet(text: str = "Change the persisted result") -> ReviewPacket:
    return ReviewPacket(
        run_id="run-review",
        request_digest=str(Digest.of_bytes(b"request")),
        configuration_snapshot_digest=str(Digest.of_bytes(b"configuration")),
        base_revision="a" * 40,
        head_revision="b" * 40,
        diff=f"+{text}",
        evidence_receipt_digests=(str(Digest.of_bytes(b"observation")),),
        ci_evidence_json=json.dumps({"status": "not_persisted"}),
        findings_json="[]",
        sections=(("Intent", json.dumps({"text": text})),),
    )


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def test_hostile_html_is_inert_and_cannot_load_worker_previews() -> None:
    hostile = '\"><script>alert(1)</script><img src="https://evil.invalid/x">&\' <iframe src="file:///tmp/worker.html">'
    packet = _packet(hostile)
    document = render_html(packet)
    parser = _Tags()
    parser.feed(document)
    assert html.escape(hostile, quote=True) in document
    assert not {tag for tag, _ in parser.tags} & {"script", "img", "iframe", "a", "form", "link"}
    policies = [attrs["content"] for tag, attrs in parser.tags
                if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"]
    assert policies == [REVIEW_CSP]
    assert "default-src 'none'" in REVIEW_CSP
    assert "script-src 'none'" in REVIEW_CSP
    assert "frame-src 'none'" in REVIEW_CSP
    assert any(attrs.get("content") == PACKET_MARKER for _, attrs in parser.tags)
    assert "cannot approve any action" in document


def test_every_terminal_control_is_visible_instead_of_executable() -> None:
    controls = "".join(
        chr(value)
        for value in (
            *range(0x20),
            *range(0x7F, 0xA0),
            0x61C,
            0x200B,
            0x200C,
            0x200D,
            0x2028,
            0x2029,
            0x2060,
            0xFEFF,
            0x200E,
            0x200F,
            *range(0x202A, 0x202F),
            *range(0x2066, 0x2070),
        )
    )
    escaped = escape_terminal(controls)
    assert all(character not in escaped for character in controls)
    assert "\\u001b" in escaped and "\\u009b" in escaped and "\\u202e" in escaped
    lines = render_terminal(_packet("\x1b[2J\x9b31m\u202eevil\nnext"))
    assert all("\n" not in line and "\x1b" not in line and "\u202e" not in line for line in lines)
    assert any("\\u001b[2J" in line for line in lines)


@pytest.mark.parametrize("changes", [
    {"run_id": "another-run"},
    {"request_digest": str(Digest.of_bytes(b"other request"))},
    {"configuration_snapshot_digest": str(Digest.of_bytes(b"other config"))},
    {"base_revision": "c" * 40},
    {"head_revision": "d" * 40},
    {"diff": "+different diff"},
    {"evidence_receipt_digests": (str(Digest.of_bytes(b"different observation")),)},
    {"ci_evidence_json": '{"status":"failed"}'},
    {"findings_json": '["repair the result"]'},
    {"protected_publication_digest": str(Digest.of_bytes(b"new matched protected paths"))},
])
def test_subject_digest_binds_every_review_input(changes: dict[str, Any]) -> None:
    packet = _packet()
    assert replace(packet, **changes).subject_digest != packet.subject_digest
    fields = packet.subject_fields()
    fields["run_id"] = "mutated copy"
    assert packet.run_id == "run-review"
    assert packet.subject_fields()["run_id"] == "run-review"


def test_presentation_lifecycle_changes_get_distinct_files_not_distinct_authority() -> None:
    packet = _packet()
    registered = replace(packet, sections=(("Risk", '{"publication":{"status":"queued"}}'),))
    resolved = replace(packet, sections=(("Risk", '{"publication":{"status":"passed"}}'),))
    assert registered.subject_digest == resolved.subject_digest == packet.subject_digest
    assert registered.digest != resolved.digest
    assert render_html(registered) != render_html(resolved)


def test_packet_and_expanded_rendering_are_bounded() -> None:
    with pytest.raises(InvalidInputError, match="1 MiB"):
        _packet("x" * (MAX_PACKET_BYTES + 1))
    packet = _packet("<" * 200_000)
    with pytest.raises(InvalidInputError, match="1 MiB"):
        render_html(packet)


def _feedback_setup(monkeypatch: pytest.MonkeyPatch, packet: ReviewPacket) -> list[ReviewReport]:
    reports: list[ReviewReport] = []
    ledger = SimpleNamespace(close=lambda: None)
    request = SimpleNamespace(implementation=SimpleNamespace(attempt_id="implement-1"))
    monkeypatch.setattr(factory_review, "_open", lambda *args, **kwargs: (ledger, request))
    monkeypatch.setattr(factory_review, "build_packet", lambda *args: packet)
    monkeypatch.setattr(factory_review, "runtime_for_run", lambda *args, **kwargs: object())
    monkeypatch.setattr(factory_review, "_projection", lambda *args: {"attempt_id": "implement-1"})

    def record_review(request: object, report: ReviewReport, **kwargs: Any) -> SimpleNamespace:
        reports.append(report)
        return SimpleNamespace(outcome=route_review(report, repair_attempt=0, repair_limit=2))

    monkeypatch.setattr(factory_review, "read_only_service", lambda **kwargs:
                        SimpleNamespace(review_implementation=record_review))
    return reports


def test_terminal_feedback_enters_existing_repair_path_and_never_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _packet()
    reports = _feedback_setup(monkeypatch, packet)
    text = '<script>approve everything</script>\x1b[2J'
    result = factory_review.feedback(tmp_path, packet.run_id,
                                     subject_digest=packet.subject_digest, text=text)
    assert result["feedback_queued"] is True
    assert result["authority_granted"] is False
    assert result["review_outcome"] == ReviewOutcome.REPAIR_REQUESTED.value
    report, = reports
    finding, = report.findings
    assert finding.actionable and not finding.blocking
    queued = json.loads(finding.finding_id)
    assert queued == {"kind": "packet_feedback", "subject_digest": packet.subject_digest,
                      "text": text}
    assert route_review(report, repair_attempt=2, repair_limit=2) is ReviewOutcome.REPAIR_EXHAUSTED


def test_feedback_rejects_stale_subject_and_empty_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _packet()
    reports = _feedback_setup(monkeypatch, packet)
    with pytest.raises(InvalidInputError, match="stale"):
        factory_review.feedback(tmp_path, packet.run_id,
                                subject_digest=str(Digest.of_bytes(b"old")), text="repair")
    with pytest.raises(InvalidInputError, match="non-blank"):
        factory_review.feedback(tmp_path, packet.run_id, subject_digest=packet.subject_digest, text=" ")
    assert reports == []


def test_existing_unresolved_feedback_is_preserved_with_its_original_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _packet()
    old_finding = json.dumps({"kind": "packet_feedback", "subject_digest": original.subject_digest,
                              "text": "first repair"})
    current = replace(original, findings_json=json.dumps([old_finding]))
    reports = _feedback_setup(monkeypatch, current)
    factory_review.feedback(tmp_path, current.run_id, subject_digest=current.subject_digest,
                            text="second repair")
    report, = reports
    assert report.findings[0].finding_id == old_finding
    assert json.loads(report.findings[1].finding_id)["subject_digest"] == current.subject_digest


def test_exact_feedback_replay_is_idempotent_without_re_resolving_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _packet()
    queued = json.dumps(
        {"kind": "packet_feedback", "subject_digest": original.subject_digest, "text": "repair"},
        sort_keys=True,
        separators=(",", ":"),
    )
    current = replace(original, findings_json=json.dumps([queued]))
    reports = _feedback_setup(monkeypatch, current)
    result = factory_review.feedback(
        tmp_path, current.run_id, subject_digest=original.subject_digest, text="repair"
    )
    assert result["already_queued"] is True
    assert result["authority_granted"] is False
    assert reports == []


def test_review_writes_a_private_digest_specific_inert_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    packet = _packet("<script>hostile</script>")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(factory_review, "read_packet", lambda *args: packet)
    first = factory_review.review(project, packet.run_id)
    path = Path(str(first["packet_path"]))
    assert path.name == "packet.html"
    assert path.parent.name == packet.digest.hex_value
    assert path.parent.parent.name == packet.run_id
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert first["subject_digest"] == packet.subject_digest
    assert first["lines"] == list(render_terminal(packet))
    assert path.read_text() == render_html(packet)
    assert factory_review.review(project, packet.run_id) == first
    path.write_text("tampered")
    with pytest.raises(InvalidInputError, match="immutable"):
        factory_review.review(project, packet.run_id)


def test_receipt_display_verifies_digest_without_treating_worker_claim_as_verdict(tmp_path: Path) -> None:
    raw = json.dumps({"category": "worker_claim", "result": "PASS"}).encode()
    digest = Digest.of_bytes(raw)
    path = tmp_path / "objects" / digest.algorithm / digest.hex_value[:2] / digest.hex_value
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    displayed = factory_review._receipt_document(tmp_path, str(digest))
    assert displayed["availability"] == "digest_verified"
    assert displayed["document"] == {"category": "worker_claim", "result": "PASS"}
    assert "accepted" not in displayed and "approved" not in displayed
    path.write_bytes(b"{}")
    with pytest.raises(InvalidInputError, match="digest mismatch"):
        factory_review._receipt_document(tmp_path, str(digest))


def test_packet_reads_durable_observation_projection_and_verified_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.cli.test_stage1 import _request

    request = _request(tmp_path)
    run_id = str(request.run.id)
    head = "b" * 40
    raw_receipt = b'{"category":"instrument_observation","capture_manifest":[]}'
    digest = Digest.of_bytes(raw_receipt)
    root = request.execution_configuration.artifact_root
    path = root / "objects" / digest.algorithm / digest.hex_value[:2] / digest.hex_value
    path.parent.mkdir(parents=True)
    path.write_bytes(raw_receipt)
    observed: dict[str, object] = {
        "head_revision": head,
        "receipt_digest": str(digest),
        "observation_receipt_digests": [str(digest)],
        "configuration_snapshot_digest": str(request.run.configuration_snapshot_digest),
        "result": "indeterminate",
    }
    states: dict[tuple[str, str], dict[str, object]] = {
        (factory_review.RUNTIME_NODE_AGGREGATE_TYPE, f"{run_id}:implement"):
            {"attempt_id": request.implementation.attempt_id},
        ("node_attempt", request.implementation.attempt_id): {"result": {"head_revision": head}},
        ("behavioral_acceptance", run_id): observed,
    }
    monkeypatch.setattr(
        factory_review, "_projection",
        lambda ledger, aggregate_type, aggregate_id: states.get((aggregate_type, aggregate_id), {}),
    )
    monkeypatch.setattr(
        factory_review, "CoordinatorRuntime",
        lambda *args, **kwargs: SimpleNamespace(
            read_node_request=lambda **kwargs: SimpleNamespace(base_revision="a" * 40)
        ),
    )
    monkeypatch.setattr(
        factory_review, "_revision", lambda path, value: head if value == "HEAD" else "a" * 40
    )
    monkeypatch.setattr(factory_review, "_git", lambda *args: "+changed behavior")
    ledger = SimpleNamespace()
    packet = factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]
    sections = dict(packet.sections)
    assert "Change one bounded behavior." in sections["Intent"]
    assert str(root) in sections["Reproduction evidence"]
    assert json.loads(sections["Observed result"])["receipts"][0]["document"]["category"] == (
        "instrument_observation"
    )
    assert packet.evidence_receipt_digests == (str(digest), str(digest))
    assert packet.head_revision == head
    assert json.loads(packet.ci_evidence_json)["availability"] == "not_persisted_for_current_head"
    # Lifecycle registration and resolution must not alter their own authorization.
    states[(factory_review.RUNTIME_NODE_AGGREGATE_TYPE, f"{run_id}:open_pr")] = {
        "status": "awaiting_human", "epoch": 3, "attempt_id": "open-pr-9",
        "operator_decision": "approved", "transitioned_at": "later",
    }
    stable = factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]
    assert stable.subject_digest == packet.subject_digest
    observed["receipt_digest"] = str(Digest.of_bytes(b"changed evaluated verdict"))
    newer = factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]
    assert newer.subject_digest != packet.subject_digest
    ci_state: dict[str, object] = {
        "head_revision": head,
        "configuration_snapshot_digest": str(request.run.configuration_snapshot_digest),
        "github_evidence": {"status": "passed"},
    }
    states[(factory_review.RUNTIME_NODE_AGGREGATE_TYPE, f"{run_id}:verify")] = ci_state
    current = factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]
    assert json.loads(current.ci_evidence_json)["availability"] == "current_head"
    ci_state["configuration_snapshot_digest"] = str(Digest.of_bytes(b"old configuration"))
    stale_config = factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]
    assert json.loads(stale_config.ci_evidence_json)["persisted"] == {}
    ci_state["configuration_snapshot_digest"] = str(request.run.configuration_snapshot_digest)
    ci_state["head_revision"] = "c" * 40
    observed["head_revision"] = "c" * 40
    stale = factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]
    assert stale.evidence_receipt_digests == ()
    stale_result = json.loads(dict(stale.sections)["Observed result"])
    assert stale_result["availability"] == "not_persisted_for_current_head"
    assert stale_result["receipts"] == []
    assert json.loads(stale.ci_evidence_json)["availability"] == "not_persisted_for_current_head"
    monkeypatch.setattr(factory_review, "_revision", lambda *args: "c" * 40)
    with pytest.raises(InvalidInputError, match="head changed"):
        factory_review.build_packet(tmp_path, ledger, request)  # type: ignore[arg-type]


def test_isolated_packet_reads_bare_intake_not_worker_replace_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enginery.domain.runner import TrustMode
    from tests.cli.test_stage1 import _request

    request = _request(tmp_path)
    workspace = request.workspace_path

    subprocess.run(
        ("git", "clone", str(request.repository_path), str(workspace)),
        check=True,
        capture_output=True,
    )
    subprocess.run(("git", "config", "user.name", "Test"), cwd=workspace, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.invalid"), cwd=workspace, check=True
    )

    def git(*arguments: str) -> str:
        return subprocess.check_output(("git", *arguments), cwd=workspace, text=True).strip()

    base = git("rev-parse", "HEAD")
    (workspace / "README").write_text("real changed behavior\n")
    git("add", "README")
    git("commit", "-m", "candidate")
    head = git("rev-parse", "HEAD")
    intake = factory_review.intake_path_for(workspace)
    git("clone", "--bare", str(workspace), str(intake))
    git("replace", head, base)
    request = replace(
        request,
        runner=replace(
            request.runner,
            trust_mode=TrustMode.ISOLATED_OCI,
            profile_digest=Digest.of_bytes(b"isolated profile"),
            image="sha256:" + "a" * 64,
        ),
    )
    run_id = str(request.run.id)
    states = {
        (factory_review.RUNTIME_NODE_AGGREGATE_TYPE, f"{run_id}:implement"): {
            "attempt_id": request.implementation.attempt_id
        },
        ("node_attempt", request.implementation.attempt_id): {"result": {"head_revision": head}},
    }
    monkeypatch.setattr(
        factory_review,
        "_projection",
        lambda ledger, kind, identity: states.get((kind, identity), {}),
    )
    monkeypatch.setattr(
        factory_review,
        "CoordinatorRuntime",
        lambda *args, **kwargs: SimpleNamespace(
            read_node_request=lambda **kwargs: SimpleNamespace(base_revision=base)
        ),
    )
    packet = factory_review.build_packet(tmp_path, SimpleNamespace(), request)
    expected = factory_review._git(intake, "diff", *factory_review.DIFF_FLAGS, base, head, "--")
    assert packet.diff == expected
    assert "+real changed behavior" in packet.diff
    assert git("diff", base, head, "--") == ""


def test_invalid_lavish_run_creates_no_presenter_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid run id must be refused before presenter creation")

    monkeypatch.setattr(factory_review, "_presenter", forbidden)
    with pytest.raises(InvalidInputError):
        factory_review.lavish_feedback(
            tmp_path, "../../outside", subject_digest="a" * 64, port=3210
        )


def test_presenter_failure_preserves_terminal_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from enginery.adapters.review_presenters import PresenterError
    from enginery.cli import factory

    monkeypatch.setattr(factory, "resolve_project", lambda value: tmp_path)
    monkeypatch.setattr(factory, "review", lambda *args: {"lines": ["terminal packet survives"]})

    def fail(*args: object, **kwargs: object) -> None:
        raise PresenterError("presenter unavailable")

    monkeypatch.setattr(factory, "present_review", fail)
    args = argparse.Namespace(
        factory_command="review",
        project=tmp_path,
        run_id="run",
        json=False,
        lavish=True,
        port=3210,
        no_open=True,
    )
    with pytest.raises(PresenterError, match="unavailable"):
        factory.run_factory(args)
    assert capsys.readouterr().out == "terminal packet survives\n"
