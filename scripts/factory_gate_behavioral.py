"""Local changed-path CLI/service/browser proof through real factory subprocesses."""

from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from enginery.cli.config_locations import private_state_directory
from factory_gate_context import GateOptions
from factory_gate_evidence import EvidenceBuilder
from factory_gate_setup import SetupGate

SCENARIO = "behavioral-acceptance"
BROWSER = (
    Path.home()
    / "Library/Caches/ms-playwright/chromium_headless_shell-1234"
    / "chrome-headless-shell-mac-arm64/chrome-headless-shell"
)
BROWSER_DIGEST = "sha256:29db62dcc2f157f06f871ca3ef573cbe62b9e14a6370556245e007b36a54bf09"
REQUIRED = {
    "changed-command": (
        "installed changed CLI produces current bound observations and a passing verdict"
    ),
    "loopback-service": "installed service response is independently asserted at the current build",
    "calibration-faults": (
        "constant result, skipped persistence and wrong build are caught at their rungs"
    ),
    "not-injected": "withheld fault is explicitly not_injected and cannot qualify acceptance",
    "infrastructure": "an observer that cannot start is inconclusive, not a caught defect",
    "zero-checks": "a new request declaring zero checks is held",
    "skipped-check": "an exit-zero skipped check cannot pass its semantic assertion",
    "qualified-browser": "qualified headless shell observes DOM and retains decodable PNG",
    "unqualified-browser": "browser digest mismatch is held with exact repair",
}

_APP = """from __future__ import annotations
import argparse, json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

def compute(a,b):
    return a + b

parser=argparse.ArgumentParser()
parser.add_argument('--serve',type=int)
parser.add_argument('--persist')
args=parser.parse_args()
value=compute(2,3)
if args.persist:
    Path(args.persist).write_text(str(value))
if args.serve is None:
    print(value)
else:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=f'<html><body><output id="result">{value}</output></body></html>'.encode()
            self.send_response(200); self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    HTTPServer(('127.0.0.1',args.serve),Handler).serve_forever()
"""
_INSTALL = """from pathlib import Path
import subprocess,sys,venv
root=Path.cwd()/'.candidate-install'
venv.EnvBuilder(with_pip=False,symlinks=True).create(root)
python=root/'bin'/'python'
query='import sysconfig;print(sysconfig.get_path("purelib"))'
site=Path(subprocess.check_output([str(python),'-c',query],text=True).strip())
(site/'sample_app.py').write_bytes(Path('sample_app.py').read_bytes())
raise SystemExit(subprocess.call([str(python),'-m','sample_app',*sys.argv[1:]]))
"""


def required_assertions(options: GateOptions) -> dict[str, str]:
    return dict(REQUIRED)


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value)


class BehavioralGate(SetupGate):
    def __init__(self, options: GateOptions, evidence: EvidenceBuilder) -> None:
        super().__init__(options, evidence)
        self.project = self.new_project("behavioral")
        self.state = private_state_directory(self.project, self.environment())
        self.database = self.state / "ledger.sqlite"
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        origin = self.root / "owner" / "repo.git"
        origin.parent.mkdir()
        self.git(self.project, "init", "--bare", "-q", str(origin))
        self.git(self.project, "remote", "add", "origin", str(origin))
        self.write_user(
            "schema_version = 1\n[executables]\n"
            f"github = {json.dumps(str(self.bin / 'gh'))}\n"
            f"harness = {json.dumps(str(self.bin / 'omp'))}\n"
            f'browser = {json.dumps(str(BROWSER))}\nbrowser_digest = "{BROWSER_DIGEST}"\n'
        )
        self.checks: list[dict[str, Any]] = []
        self.run_counter = 0

    def record(self, key: str, passed: bool, observed: Any) -> None:
        self.evidence.assertion(
            key,
            passed=passed,
            commands=list(self.commands),
            observed=observed,
            expected=REQUIRED[key],
            observer={"kind": "host CLI and retained receipt inspector"},
        )

    def cli_json(self, *args: str) -> tuple[int, dict[str, Any]]:
        process = self.cli(*args)
        try:
            payload = json.loads(process.stdout)
        except ValueError as error:
            raise RuntimeError(
                f"CLI failed ({process.returncode}): {process.stderr[:500]}"
            ) from error
        return process.returncode, dict(payload)

    def bind(self, checks: list[dict[str, Any]]) -> str:
        self.run_counter += 1
        run_id = f"behavior-{self.run_counter}"
        policy = self.project / ".enginery" / "project.toml"
        policy.parent.mkdir(exist_ok=True)
        policy.write_text(
            'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
            '[validation]\ncommands = ["true"]\n[acceptance]\nchecks = ' + _toml(checks) + "\n"
        )
        (self.project / "sample_app.py").write_text(_APP.replace("return a + b", "return 0"))
        (self.project / "install_sample.py").write_text(_INSTALL)
        self.git(self.project, "add", ".")
        self.git(self.project, "commit", "--allow-empty", "-qm", "operator base assertions")
        base = self.git(self.project, "rev-parse", "HEAD")
        workspace = self.state / "runs" / run_id / "workspace"
        request = self.state / "runs" / run_id / "request.json"
        request.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        code, built = self.cli_json(
            "stage1",
            "build-request",
            "--output",
            str(request),
            "--run-id",
            run_id,
            "--repository",
            "owner/repo",
            "--external-reference",
            "owner/repo#1",
            "--base-revision",
            base,
            "--repository-path",
            str(self.project),
            "--workspace-path",
            str(workspace),
            "--artifact-root",
            str(self.state / "artifacts"),
        )
        if code:
            raise RuntimeError(f"request refused: {built}")
        code, started = self.cli_json(
            "stage1",
            "start",
            "--database",
            str(self.database),
            "--owner",
            "factory",
            "--request",
            str(request),
        )
        if code:
            raise RuntimeError(f"bound start refused: {started}")
        self.git(self.project, "clone", "--quiet", "--no-local", str(self.project), str(workspace))
        self.git(workspace, "config", "user.name", "local candidate fixture")
        self.git(workspace, "config", "user.email", "fixture@example.invalid")
        (workspace / "sample_app.py").write_text(_APP)
        self.git(workspace, "add", "sample_app.py")
        self.git(workspace, "commit", "-qm", "implement actual addition")
        return run_id

    def factory(self, command: str, run_id: str, *args: str) -> tuple[int, dict[str, Any]]:
        return self.cli_json(
            "factory", command, run_id, "--project", str(self.project), "--json", *args
        )

    def artifact(self, digest: str) -> dict[str, Any]:
        algorithm, value = digest.split(":", 1)
        path = self.state / "artifacts" / "objects" / algorithm / value[:2] / value
        return dict(json.loads(path.read_bytes()))

    def cleanup(self) -> None:
        artifacts = self.state / "artifacts"
        if artifacts.is_dir():
            shutil.copytree(artifacts, self.options.raw_dir / "artifacts", dirs_exist_ok=True)
        super().cleanup()


def prepare(
    options: GateOptions,
    evidence: EvidenceBuilder,
    scrubber: object,
    engine_factory: object,
    *,
    need_relay: bool,
) -> BehavioralGate:
    return BehavioralGate(options, evidence)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _rows(calibration: dict[str, Any]) -> list[dict[str, Any]]:
    return list(calibration.get("calibration", {}).get("results", []))


def run(gate: BehavioralGate) -> None:
    command = {
        "name": "changed-cli",
        "observer": "command",
        "argv": [sys.executable, "install_sample.py", "--persist", "result.txt"],
        "assertions": [
            {"kind": "stdout", "equals": "5\n"},
            {"kind": "produced_file", "path": "result.txt", "equals": "5"},
        ],
        "fault_classes": ["constant_result", "skipped_persistence", "wrong_build"],
    }
    port = _port()
    service = {
        "name": "changed-service",
        "observer": "service",
        "argv": [sys.executable, "install_sample.py", "--serve", str(port)],
        "url": f"http://127.0.0.1:{port}/",
        "assertions": [{"kind": "body", "contains": '<output id="result">5</output>'}],
        "fault_classes": ["constant_result", "wrong_build"],
    }
    run_id = gate.bind([command, service])
    calibration_code, calibrated = gate.factory("calibrate", run_id)
    code, verdict = gate.factory("check", run_id)
    gate.record(
        "changed-command",
        calibration_code == code == 0 and verdict.get("result") == "pass",
        verdict,
    )
    receipt = gate.artifact(str(verdict["receipt_digest"]))
    observations = [gate.artifact(item) for item in verdict.get("observation_receipt_digests", [])]
    gate.record(
        "loopback-service",
        code == 0
        and any(
            item.get("observer", {}).get("kind") == "service"
            and item.get("category") == "instrument_observation"
            for item in observations
        )
        and receipt.get("head_revision") is not None,
        {"verdict": receipt, "observations": observations},
    )
    rows = _rows(calibrated)
    caught = {item.get("fault_class") for item in rows if item.get("classification") == "caught"}
    rungs = {item.get("rung") for item in rows if item.get("classification") == "caught"}
    gate.record(
        "calibration-faults",
        {"constant_result", "skipped_persistence", "wrong_build"} <= caught
        and "binding_identity" in rungs
        and "evaluator" in rungs,
        calibrated,
    )
    _, withheld = gate.factory("calibrate", run_id, "--without-fault", "constant_result")
    code, rejected = gate.factory("check", run_id)
    gate.record(
        "not-injected",
        any(
            item.get("classification") == "not_injected"
            and item.get("fault_class") == "constant_result"
            for item in _rows(withheld)
        )
        and code != 0
        and rejected.get("result") != "pass",
        {"calibration": withheld, "verdict": rejected},
    )
    missing = dict(command, name="missing", argv=["enginery-nonexistent-observer-binary"])
    missing_run = gate.bind([missing])
    _, unavailable = gate.factory("calibrate", missing_run)
    gate.record(
        "infrastructure",
        any(item.get("classification") == "inconclusive" for item in _rows(unavailable)),
        unavailable,
    )
    empty_run = gate.bind([])
    code, empty = gate.factory("status", empty_run)
    gate.record(
        "zero-checks",
        code != 0 and empty.get("hold", {}).get("reason") == "no_behavioral_checks",
        empty,
    )
    skipped = dict(command, name="skipped", argv=[sys.executable, "-c", "print('1 skipped')"])
    skipped_run = gate.bind([skipped])
    gate.factory("calibrate", skipped_run)
    code, skipped_result = gate.factory("check", skipped_run)
    gate.record(
        "skipped-check", code != 0 and skipped_result.get("result") != "pass", skipped_result
    )
    browser = dict(
        service,
        name="browser",
        observer="browser",
        assertions=[{"kind": "dom", "selector": "#result", "equals": "5"}],
        fault_classes=["wrong_build"],
    )
    browser_run = gate.bind([browser])
    gate.factory("calibrate", browser_run)
    code, browser_result = gate.factory("check", browser_run)
    browser_observations = [
        gate.artifact(item) for item in browser_result.get("observation_receipt_digests", [])
    ]
    captures = [
        capture
        for item in browser_observations
        for capture in item.get("capture_manifest", {}).get("artifacts", [])
    ]
    gate.record(
        "qualified-browser",
        BROWSER.is_file()
        and code == 0
        and browser_result.get("result") == "pass"
        and any(
            item.get("media_type") == "image/png"
            and item.get("decode", {}).get("status") == "decodable"
            for item in captures
        ),
        {"result": browser_result, "observations": browser_observations},
    )
    user = gate.user_file()
    user.write_text(user.read_text().replace(BROWSER_DIGEST, "sha256:" + "0" * 64))
    code, mismatch = gate.factory("check", browser_run)
    gate.record(
        "unqualified-browser",
        code != 0 and "browser_observer_unqualified" in json.dumps(mismatch),
        mismatch,
    )
    gate.effects.update({"hosted_inference_calls": 0, "github_mutations": 0})


def finalize(gate: BehavioralGate) -> None:
    pass
