"""Exercise terminal authority with real CLI subprocesses, durable truth and one OS PTY.

GitHub and the implementation harness are recording local fixtures. The installed candidate
and trusted command observer are real. No challenge is serialized into retained evidence.
HTTP coverage submits to a local transport fixture that forwards to the real CLI; it is not
an Enginery HTTP API. Configured Lavish coverage additionally probes its real local listener.
"""

from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from enginery.adapters.review_presenters import LavishPresenter, PresenterError
from enginery.cli.config_locations import read_user_layer
from factory_gate_context import GateHoldError, GateOptions
from factory_gate_evidence import EvidenceBuilder
from factory_gate_manual_path import (
    _GH,
    ISSUE,
    OPERATOR_PRINCIPAL,
    PUBLICATION_ACTION,
    ManualPathGate,
    TerminalResult,
    step_collect,
    step_prepare,
)
from factory_gate_manual_path import (
    REQUIRED as MANUAL_REQUIRED,
)

SCENARIO = "review-authority"
MAX_BYTES = 1024 * 1024
HOSTILE = '<script>window.gateInjected=1</script><img src="https://invalid.test/x" onerror="1">'
CONTROLS = "\x1b[31mworker\x7f\x85\u202e"
REQUIRED = {
    "collected-current-candidate": "real installed changed-candidate validation is collected",
    "safe-review-packet": "hostile HTML and terminal controls are inert in both review forms",
    "terminal-feedback-no-authority": "terminal feedback queues bound findings and no decision",
    "report-alone-no-publication": "a review report alone cannot release publication",
    "noninteractive-refused": "a pipe cannot issue or submit a terminal decision challenge",
    "json-no-secret": "interactive JSON contains no secret; only one separate terminal line does",
    "forged-principal-refused": "an unregistered principal cannot consume a challenge",
    "forged-challenge-refused": "an invented challenge cannot record a decision",
    "wrong-action-refused": "a challenge cannot authorize another action",
    "wrong-digest-refused": "a challenge cannot authorize a different digest",
    "stale-subject-refused": "new findings invalidate the old subject and challenge",
    "expired-challenge-refused": "a challenge beyond its real expiry cannot authorize anything",
    "fresh-decision-once": "a current registered-human PTY decision persists exactly once",
    "replayed-challenge-refused": "a consumed challenge cannot create another decision",
    "http-no-authority": "an HTTP submission forwarded to real CLI ingress grants no authority",
    "changed-evidence-supersedes": "new receipts supersede authority and publication refuses it",
    "authorized-publication": "a new current-subject decision permits exactly one local PR",
    "no-review-triggers": "no review request, mention, merge, release, deploy or hosted call",
    "challenge-secrecy-history": "retained history survives without any plaintext challenge",
}
LAVISH_REQUIRED = {
    "lavish-feedback-no-authority": "real configured Lavish queues findings and no decision",
    "lavish-http-no-authority": "the actual Lavish listener refuses decision-shaped HTTP posts",
}

_VALIDATOR = """from pathlib import Path
import json, subprocess, sys, venv
root = Path.cwd() / '.candidate-install'
venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
python = root / 'bin' / 'python'
site_code = 'import sysconfig;print(sysconfig.get_path("purelib"))'
site = Path(subprocess.check_output([str(python), '-c', site_code], text=True).strip())
(site / 'candidate.py').write_bytes(Path('candidate.py').read_bytes())
result = subprocess.run([str(python), '-I', '-c',
                         'import candidate;print(candidate.compute(2,3))'],
                        capture_output=True, text=True, check=True)
assert result.stdout.strip() == '5', result.stdout
Path('validation-result.json').write_text(json.dumps({'result': 5,
                                                     'installed_python': str(python)}))
print('5')
"""
_HARNESS = r"""#!__PYTHON__
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args in (["--help"], ["--version"]):
    print("omp v18.10.3"); raise SystemExit(0)
workspace = Path.cwd()
with (Path(__file__).resolve().parent.parent / "harness-launches.jsonl").open("a") as log:
    log.write(json.dumps({"pid": os.getpid(), "workspace": str(workspace), "argv": args,
                         "hosted_session_requested": False}) + "\n")
print(json.dumps({"type": "session"}), flush=True)
print(json.dumps({"type": "agent_start"}), flush=True)
(workspace / "candidate.py").write_text("def compute(a, b):\n    return a + b\n")
(workspace / "hostile-worker.txt").write_text(__HOSTILE__ + "\n" + __CONTROLS__ + "\n")
def git(*arguments):
    subprocess.run(["git", "-c", "user.name=gate candidate", "-c",
                    "user.email=gate@example.invalid", *arguments],
                   check=True, capture_output=True)
git("checkout", "-b", "enginery/" + workspace.parent.name)
git("add", "candidate.py", "hostile-worker.txt")
git("commit", "-qm", "feat: implement real local addition")
print(json.dumps({"type": "agent_end"}), flush=True)
"""


def operator_lavish() -> str | None:
    """Read operator selection before constructing the disposable HOME."""
    user = read_user_layer()
    programs = None if user is None else user.operator_executables
    return None if programs is None or programs.lavish is None else str(programs.lavish)


def required_assertions(options: GateOptions) -> dict[str, str]:
    required = dict(REQUIRED)
    if operator_lavish() is not None:
        required.update(LAVISH_REQUIRED)
    return required


class ReviewAuthorityGate(ManualPathGate):
    def __init__(self, options: GateOptions, evidence: EvidenceBuilder) -> None:
        self.lavish = operator_lavish()
        self.history_retained = False
        self.report_counter = 0
        self.presenter_session: tuple[Path, int] | None = None
        super().__init__(options, evidence)

    def _write_stand_ins(self) -> None:
        super()._write_stand_ins()
        github = _GH.replace(
            '"title": "Bounded local change " + str(number)',
            '"title": ' + repr(HOSTILE) + " + str(number)",
        )
        (self.bin / "gh").write_text(github.replace("__PYTHON__", sys.executable))
        harness = (
            _HARNESS.replace("__PYTHON__", sys.executable)
            .replace("__HOSTILE__", repr(HOSTILE))
            .replace("__CONTROLS__", repr(CONTROLS))
        )
        (self.bin / "omp").write_text(harness)

    def preferences(self, *, ttl: int = 1800, lavish: str | None = None) -> None:
        super().preferences(ttl=ttl, lavish=self.lavish)

    def acceptance_checks(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "installed-addition",
                "observer": "command",
                "argv": [sys.executable, "validate_candidate.py"],
                "assertions": [{"kind": "stdout", "equals": "5\n"}],
                "fault_classes": ["wrong_build"],
            }
        ]

    def record(self, key: str, passed: bool, observed: Any, observer: Any = None) -> None:
        if key in MANUAL_REQUIRED:
            if not passed:
                raise RuntimeError("manual-path fixture prerequisite failed: " + key)
            return
        self.evidence.assertion(
            key,
            passed=passed,
            commands=list(self.commands),
            observed=observed,
            expected={**REQUIRED, **LAVISH_REQUIRED}[key],
            observer=observer or {"kind": "real CLI, OS PTY and durable SQLite inspector"},
        )
        self.recorded.add(key)

    def decisions(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM human_decisions WHERE run_id = ? ORDER BY rowid",
                    (self.run_id,),
                )
            ]

    def report_findings(self) -> subprocess.CompletedProcess[str]:
        self.report_counter += 1
        path = self.root / "review-findings.json"
        path.write_text(
            json.dumps(
                {
                    "producer": "local-implementation",
                    "reviewer": f"human-terminal-{self.report_counter}",
                    "findings": [],
                }
            )
        )
        return self.cli(
            "stage1",
            "review",
            "--database",
            str(self.database),
            "--owner",
            "factory",
            "--run-id",
            self.run_id,
            "--report",
            str(path),
            "--repair-attempt",
            "0",
        )

    def pipe_decision(
        self,
        issued: TerminalResult,
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        if self.terminal is None:
            raise RuntimeError("a real challenge must precede noninteractive submissions")
        args = [
            "factory",
            "decide",
            self.run_id,
            "--project",
            str(self.project),
            "--action",
            PUBLICATION_ACTION,
            "--subject-digest",
            issued.subject or "",
            "--challenge",
            issued.challenge or "",
            "--principal-id",
            OPERATOR_PRINCIPAL,
        ]
        process = subprocess.run(
            [sys.executable, "-m", "enginery.cli.main", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.environment(extra_env),
            cwd=self.root,
            timeout=180,
        )
        safe_args = [self.terminal.scrub(arg) for arg in args]
        stdout, stderr = self.terminal.scrub(process.stdout), self.terminal.scrub(process.stderr)
        self.commands.append("enginery " + shlex.join(safe_args))
        self.completed.append(
            {
                "argv": safe_args,
                "exit_code": process.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "real_pty": False,
            }
        )
        return process.returncode, stdout + stderr

    def packet(self) -> Path:
        packets = list((self.state / "review" / self.run_id).glob("*/packet.html"))
        if not packets:
            raise RuntimeError("trusted review did not retain its HTML packet")
        return max(packets, key=lambda path: path.stat().st_mtime_ns)

    def cleanup(self) -> None:
        try:
            if self.presenter_session is not None and self.lavish is not None:
                packet, port = self.presenter_session
                try:
                    with patch.dict(os.environ, self.environment(), clear=True):
                        LavishPresenter(self.lavish, self.state, port, no_open=True).end(packet)
                except PresenterError as error:
                    self.evidence.abort(f"configured presenter cleanup refused: {error}")
                self.presenter_session = None
            if self.state.exists():
                destination = self.options.raw_dir / "project-history"
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                for name in ("artifacts", "review", "runs"):
                    source = self.state / name
                    if source.is_dir():
                        shutil.copytree(
                            source,
                            destination / name,
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("workspace", ".candidate-install"),
                        )
                if self.database.exists():
                    with (
                        sqlite3.connect(self.database) as source,
                        sqlite3.connect(destination / "ledger.sqlite") as backup,
                    ):
                        source.backup(backup)
                    self.history_retained = True
        finally:
            super().cleanup()


def prepare(
    options: GateOptions,
    evidence: EvidenceBuilder,
    scrubber: object,
    engine_factory: object,
    *,
    need_relay: bool,
) -> ReviewAuthorityGate:
    if shutil.which("git") is None:
        raise GateHoldError("git is required for the local review-authority scenario")
    return ReviewAuthorityGate(options, evidence)


def _fixture(gate: ReviewAuthorityGate) -> None:
    step_prepare(gate)
    (gate.project / "candidate.py").write_text("def compute(a, b):\n    return 0\n")
    (gate.project / "validate_candidate.py").write_text(_VALIDATOR)
    policy = gate.project / ".enginery" / "project.toml"
    command = shlex.join([sys.executable, "validate_candidate.py"])
    text = policy.read_text().replace(
        'commands = ["true"]',
        "commands = [" + json.dumps(command) + "]",
    )
    policy.write_text(text)
    gate.git(gate.project, "add", ".")
    gate.git(gate.project, "commit", "-qm", "operator declares installed candidate validation")
    branch = gate.git(gate.project, "symbolic-ref", "--short", "HEAD")
    gate.git(gate.project, "push", "-q", "origin", branch)
    step_collect(gate)
    code, result = gate.factory("run", ISSUE, "--wait-seconds", "30")
    gate.run_id = gate.index()["repositories"]["owner/repo"]["issues"][ISSUE]["run_id"]
    request = json.loads(gate.request_path().read_text())
    gate.workspace = Path(request["workspace_path"])
    states = {node["node_id"]: node["status"] for node in result.get("nodes", [])}
    installed = gate.workspace / "validation-result.json"
    observed = json.loads(installed.read_text()) if installed.exists() else {}
    gate.record(
        "collected-current-candidate",
        code == 0
        and states.get("implement") == "passed"
        and states.get("validate") == "passed"
        and observed.get("result") == 5,
        {"run": result, "installed_validation": observed},
    )
    if not installed.exists():
        raise RuntimeError("real installed candidate validation was not collected")
    calibration_code, _ = gate.factory("calibrate", gate.run_id)
    check_code, checked = gate.factory("check", gate.run_id)
    if calibration_code != 0 or check_code != 0 or checked.get("result") != "pass":
        raise RuntimeError("current behavioral evidence did not pass: " + json.dumps(checked))


def _packet_and_feedback(gate: ReviewAuthorityGate) -> None:
    report = gate.report_findings()
    code, held = gate.publication_hold()
    report_json = json.loads(report.stdout) if report.stdout.strip() else {}
    gate.record(
        "report-alone-no-publication",
        report.returncode == 0
        and report_json.get("findings_recorded") is True
        and not (gate.bin / "gh-pr.json").exists()
        and not gate.decisions()
        and "decision" in json.dumps(held).lower(),
        {"report": report_json, "resume_exit": code, "resume": held},
    )
    issued = gate.challenge()
    packet = gate.packet().read_text()
    gate.record(
        "safe-review-packet",
        html.escape(HOSTILE) in packet
        and HOSTILE not in packet
        and HOSTILE in issued.text
        and not any(char in issued.text for char in ("\x1b", "\x7f", "\x85", "\u202e"))
        and "Content-Security-Policy" in packet
        and "script-src" in packet
        and not re.search(r"<(?:script|img)\b", packet, re.IGNORECASE),
        {
            "terminal": issued.text,
            "html": packet,
            "packet_sha256": hashlib.sha256(packet.encode()).hexdigest(),
        },
    )
    before = gate.decisions()
    code, queued = gate.factory(
        "feedback",
        gate.run_id,
        "--subject-digest",
        issued.subject or "",
        "--text",
        "repair requested; this is not approval " + HOSTILE,
    )
    gate.record(
        "terminal-feedback-no-authority",
        code == 0
        and queued.get("feedback_queued") is True
        and queued.get("authority_granted") is False
        and gate.decisions() == before
        and not (gate.bin / "gh-pr.json").exists(),
        {"feedback": queued, "decision_count": len(gate.decisions())},
    )
    stale = gate.decide(issued)
    gate.record(
        "stale-subject-refused",
        stale.code != 0 and "decision_subject_stale" in stale.text and gate.decisions() == before,
        {"exit": stale.code, "text": stale.text},
    )
    _clear_findings(gate)


def _clear_findings(gate: ReviewAuthorityGate) -> None:
    report = gate.report_findings()
    if report.returncode != 0:
        raise RuntimeError("terminal findings report was refused: " + report.stderr)
    gate.publication_hold()


def _refusals_and_success(gate: ReviewAuthorityGate) -> TerminalResult:
    issued = gate.challenge()
    refusals: tuple[tuple[str, dict[str, str], str], ...] = (
        ("forged-principal-refused", {"principal": "forged-human"}, "decision_principal_mismatch"),
        ("forged-challenge-refused", {"challenge": "0" * 64}, "decision_challenge_unknown"),
        ("wrong-action-refused", {"action": "release.publish"}, "decision_action_mismatch"),
        ("wrong-digest-refused", {"subject": "sha256:" + "0" * 64}, "decision_subject_mismatch"),
    )
    for key, kwargs, error in refusals:
        result = gate.decide(issued, **kwargs)
        gate.record(
            key,
            result.code != 0 and error in result.text and not gate.decisions(),
            {"exit": result.code, "text": result.text},
        )
    review_code, review = gate.factory("review", gate.run_id)
    pipe_code, pipe_text = gate.pipe_decision(issued)
    gate.record(
        "noninteractive-refused",
        review_code == 0
        and review.get("challenge_issued") is False
        and "challenge" not in review
        and "Decision challenge:" not in json.dumps(review)
        and pipe_code != 0
        and "decision_authentication_required" in pipe_text
        and not gate.decisions(),
        {"review": review, "decide_exit": pipe_code, "decide": pipe_text},
    )
    json_review = gate.interactive("review", "--json")
    json_start = json_review.text.find("{")
    json_payload: object = None
    if json_start >= 0:
        with contextlib.suppress(ValueError):
            json_payload, _ = json.JSONDecoder().raw_decode(json_review.text[json_start:])
    gate.record(
        "json-no-secret",
        json_review.code == 0
        and json_review.challenge is not None
        and isinstance(json_payload, dict)
        and json_payload.get("challenge_issued") is True
        and json_payload.get("subject_digest") == issued.subject
        and "challenge" not in json_payload
        and "<challenge-sha256:" not in json.dumps(json_payload)
        and "Decision challenge:" not in json.dumps(json_payload)
        and json_review.text.count("<challenge-sha256:") == 1,
        {"terminal": json_review.text, "json": json_payload},
    )
    gate.preferences(ttl=1)
    expiring = gate.challenge()
    if gate.terminal is None or expiring.challenge is None:
        raise RuntimeError("expiry proof requires an issued terminal challenge")
    digest = hashlib.sha256(expiring.challenge.encode()).hexdigest()
    with sqlite3.connect(gate.database) as connection:
        row = connection.execute(
            "SELECT expires_at FROM human_decision_challenges WHERE challenge_hash = ?",
            (digest,),
        ).fetchone()
    if row is None:
        raise RuntimeError("issued challenge was not durably stored")
    expires = datetime.fromisoformat(row[0])
    time.sleep(max(0, (expires - datetime.now(UTC)).total_seconds()) + 0.1)
    expired = gate.decide(expiring)
    gate.record(
        "expired-challenge-refused",
        expired.code != 0 and "decision_challenge_expired" in expired.text and not gate.decisions(),
        {"exit": expired.code, "text": expired.text, "expires_at": row[0]},
    )
    gate.preferences()
    current = gate.challenge()
    success = gate.decide(current)
    decisions = gate.decisions()
    gate.record(
        "fresh-decision-once",
        success.code == 0
        and len(decisions) == 1
        and decisions[0]["principal_id"] == OPERATOR_PRINCIPAL
        and decisions[0]["action"] == PUBLICATION_ACTION
        and decisions[0]["subject_digest"] == current.subject
        and decisions[0]["os_uid"] == os.getuid()
        and decisions[0]["tty_name"] == os.ttyname(gate.terminal.slave)
        and decisions[0]["expires_at"] > decisions[0]["decided_at"]
        and "findings_recorded" not in success.text,
        {"exit": success.code, "text": success.text, "decisions": decisions},
    )
    replay = gate.decide(current)
    gate.record(
        "replayed-challenge-refused",
        replay.code != 0
        and "decision_challenge_replayed" in replay.text
        and gate.decisions() == decisions,
        {"exit": replay.code, "text": replay.text},
    )
    return current


def _http_refusal(gate: ReviewAuthorityGate, issued: TerminalResult) -> None:
    observations: list[dict[str, object]] = []

    class ForwardToCli(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BYTES:
                self.send_error(413)
                return
            document = json.loads(self.rfile.read(length))
            candidate = TerminalResult(
                0,
                "",
                challenge=document["challenge"],
                subject=document["subject_digest"],
            )
            code, text = gate.pipe_decision(
                candidate,
                extra_env={
                    "HTTP_ORIGIN": self.headers.get("Origin", ""),
                    "REQUEST_METHOD": "POST",
                    "REMOTE_ADDR": self.client_address[0],
                },
            )
            observations.append({"exit": code, "text": text})
            body = json.dumps(observations[-1]).encode()
            self.send_response(403 if code else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, message: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ForwardToCli)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = gate.decisions()
    try:
        status = _post(
            server.server_port,
            "/factory/decide",
            {
                "subject_digest": issued.subject,
                "challenge": issued.challenge,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    gate.commands.append("HTTP POST local CLI-transport fixture /factory/decide")
    gate.record(
        "http-no-authority",
        status == 403
        and len(observations) == 1
        and observations[0]["exit"] != 0
        and "decision_authentication_required" in str(observations[0]["text"])
        and gate.decisions() == before,
        {
            "transport": "local HTTP-to-real-CLI fixture (not an Enginery API)",
            "status": status,
            "cli": observations,
            "decision_count_before": len(before),
            "decision_count_after": len(gate.decisions()),
        },
    )


def _supersession_and_publication(gate: ReviewAuthorityGate, issued: TerminalResult) -> None:
    code, new_evidence = gate.factory("check", gate.run_id)
    resume_code, refused = gate.publication_hold("decision_superseded")
    decisions = gate.decisions()
    gate.record(
        "changed-evidence-supersedes",
        code == 0
        and new_evidence.get("result") == "pass"
        and len(decisions) == 1
        and bool(decisions[0]["superseded_by"])
        and decisions[0]["subject_digest"] == issued.subject
        and not (gate.bin / "gh-pr.json").exists()
        and "decision" in json.dumps(refused).lower(),
        {
            "new_evidence": new_evidence,
            "resume_exit": resume_code,
            "resume": refused,
            "decisions": decisions,
        },
    )
    fresh = gate.challenge()
    approved = gate.decide(fresh)
    (gate.bin / "release-pr").touch()
    publish_code, published = gate.factory("resume", gate.run_id, "--wait-seconds", "30")
    pr_path = gate.bin / "gh-pr.json"
    posts = [
        call for call in gate.gh_calls() if "POST" in call and "repos/owner/repo/pulls" in call
    ]
    gate.record(
        "authorized-publication",
        approved.code == 0
        and publish_code == 0
        and pr_path.is_file()
        and len(gate.decisions()) == 2
        and len(posts) == 1
        and fresh.subject != issued.subject,
        {
            "decision": approved.text,
            "resume": published,
            "pull_request": json.loads(pr_path.read_text()) if pr_path.exists() else None,
        },
    )
    if not pr_path.exists():
        raise RuntimeError("authorized CLI did not publish the local fixture PR")


def _post(port: int, path: str, payload: Mapping[str, object]) -> int:
    connection = HTTPConnection("127.0.0.1", port, timeout=180)
    try:
        connection.request(
            "POST",
            path,
            json.dumps(payload),
            {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        )
        response = connection.getresponse()
        response.read(MAX_BYTES)
        return response.status
    finally:
        connection.close()


def _lavish_feedback(gate: ReviewAuthorityGate) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    opened = gate.interactive("review", "--lavish", "--no-open", "--port", str(port))
    if opened.code == 0:
        gate.presenter_session = (gate.packet(), port)
    if opened.code != 0 or opened.subject is None or opened.challenge is None:
        raise GateHoldError("configured Lavish could not open the real safe local packet")
    packet = gate.packet()
    key = hashlib.sha256(str(packet.resolve()).encode()).hexdigest()[:16]
    before = gate.decisions()
    forged_statuses = [
        _post(
            port,
            path,
            {
                "run_id": gate.run_id,
                "action": PUBLICATION_ACTION,
                "subject_digest": opened.subject,
                "principal_id": OPERATOR_PRINCIPAL,
                "challenge": opened.challenge,
            },
        )
        for path in ("/factory/decide", "/api/decisions", f"/api/{key}/decide")
    ]
    gate.commands.append("HTTP POST configured Lavish loopback decision paths")
    gate.record(
        "lavish-http-no-authority",
        all(status in {400, 403, 404, 405} for status in forged_statuses)
        and gate.decisions() == before,
        {
            "listener": "real configured Lavish",
            "statuses": forged_statuses,
            "decision_count_before": len(before),
            "decision_count_after": len(gate.decisions()),
        },
    )
    status = _post(
        port,
        f"/api/{key}/prompts",
        {
            "prompts": [
                {
                    "uid": "authority-gate-feedback",
                    "prompt": "Repair this result " + HOSTILE,
                    "target": {"type": "text"},
                }
            ],
            "dom_snapshot": "",
        },
    )
    gate.commands.append("HTTP POST configured Lavish loopback queued prompt")
    code, result = gate.factory(
        "feedback",
        gate.run_id,
        "--subject-digest",
        opened.subject,
        "--lavish-poll",
        "--port",
        str(port),
    )
    gate.presenter_session = None  # The real feedback command ends its presenter in finally.
    gate.record(
        "lavish-feedback-no-authority",
        200 <= status < 300
        and code == 0
        and result.get("feedback_queued") is True
        and result.get("authority_granted") is False
        and gate.decisions() == before
        and not (gate.bin / "gh-pr.json").exists(),
        {
            "annotation_http_status": status,
            "feedback": result,
            "decision_count_before": len(before),
            "decision_count_after": len(gate.decisions()),
        },
    )
    _clear_findings(gate)


def _capture_boundaries(gate: ReviewAuthorityGate) -> None:
    calls = gate.gh_calls()
    mutations = [
        call for call in calls if "--method" in call and call[call.index("--method") + 1] != "GET"
    ]
    forbidden = [
        call
        for call in mutations
        if any(
            suffix in arg
            for arg in call
            for suffix in (
                "/requested_reviewers",
                "/reviews",
                "/merge",
                "/releases",
                "/deployments",
            )
        )
        or any(arg.startswith("body=") and re.search(r"(?<![\w.])@[\w-]+", arg) for arg in call)
    ]
    engine_log = gate.bin / "container-engine-calls.jsonl"
    posts = [call for call in mutations if "POST" in call and "repos/owner/repo/pulls" in call]
    gate.effects.update(
        hosted_inference_calls=0,
        hosted_sessions=0,
        github_reads=len(calls) - len(mutations),
        github_mutations=len(mutations),
        pull_request_creations=len(posts),
        review_requests=len(forbidden),
        container_engine_calls=(
            len(engine_log.read_text().splitlines()) if engine_log.exists() else 0
        ),
    )
    gate.record(
        "no-review-triggers",
        not forbidden
        and len(posts) == 1
        and not engine_log.exists()
        and all(not launch["hosted_session_requested"] for launch in gate.launches()),
        {"forbidden_calls": forbidden, "effects": gate.effects, "gh_calls": calls},
    )
    gate.raw_write("commands.json", gate.completed)
    gate.raw_write("gh-calls.json", calls)
    gate.raw_write("harness-launches.json", gate.launches())


def run(gate: ReviewAuthorityGate) -> None:
    try:
        _fixture(gate)
        _packet_and_feedback(gate)
        if gate.lavish is not None:
            _lavish_feedback(gate)
        else:
            gate.evidence.not_requested["lavish"] = (
                "operator did not configure [executables].lavish; terminal feedback and real "
                "HTTP-to-CLI authentication refusal remain required"
            )
        current = _refusals_and_success(gate)
        _http_refusal(gate, current)
        _supersession_and_publication(gate, current)
    finally:
        _capture_boundaries(gate)


def finalize(gate: ReviewAuthorityGate) -> None:
    """Inspect original retained history after the disposable HOME is removed."""
    if gate.terminal is None or not gate.history_retained:
        return  # The dispatcher records the unexercised secrecy assertion as not-run/fail.
    leaks: list[str] = []
    secrets = [secret.encode() for secret in gate.terminal.secrets]
    retained = gate.options.raw_dir / "project-history" / "ledger.sqlite"
    for path in gate.options.raw_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                previous = b""
                while chunk := stream.read(MAX_BYTES):
                    data = previous + chunk
                    if any(secret in data for secret in secrets):
                        leaks.append(str(path.relative_to(gate.options.raw_dir)))
                        break
                    previous = data[-63:]
    with sqlite3.connect(retained) as connection:
        connection.row_factory = sqlite3.Row
        history = [dict(row) for row in connection.execute("SELECT * FROM human_decisions")]
    gate.record(
        "challenge-secrecy-history",
        not leaks
        and bool(secrets)
        and bool(history)
        and all("challenge_hash" in row for row in history)
        and not gate.root.exists(),
        {
            "plaintext_leak_paths": leaks,
            "issued_challenges": len(secrets),
            "decisions": history,
            "retained_ledger": "project-history/ledger.sqlite",
        },
    )
