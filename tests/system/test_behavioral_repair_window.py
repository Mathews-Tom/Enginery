"""Real CLI regression for the default behavioral repair review window."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

from factory_gate_context import GateOptions
from factory_gate_manual_path import ManualPathGate, TerminalSession, step_prepare
from factory_gate_review_authority import _HARNESS, _VALIDATOR


class RepairGate(ManualPathGate):
    def _write_stand_ins(self) -> None:
        super()._write_stand_ins()
        body = (
            _HARNESS.replace("__PYTHON__", sys.executable)
            .replace("__HOSTILE__", repr("benign worker note"))
            .replace("__CONTROLS__", repr("none"))
        )
        body = body.replace(
            '(workspace / "candidate.py").write_text("def compute(a, b):\\n    return a + b\\n")',
            'count = Path(__file__).resolve().parent / "launch-count"\n'
            "n = int(count.read_text()) if count.exists() else 0\n"
            "count.write_text(str(n + 1))\n"
            '(workspace / "candidate.py").write_text("def compute(a, b):\\n    return a + b"'
            ' + (" + 1" if n == 0 else "") + "\\n")',
        ).replace('git("checkout", "-b",', 'git("checkout", "-B",')
        assert "launch-count" in body
        (self.bin / "omp").write_text(body, encoding="utf-8")
        (self.bin / "release-pr").touch()
        (self.bin / "release-worker").touch()

    def acceptance_checks(self):
        return [
            {
                "name": "installed-addition",
                "observer": "command",
                "argv": [sys.executable, "validate_candidate.py"],
                "assertions": [{"kind": "stdout", "equals": "5\n"}],
                "fault_classes": ["constant_result", "wrong_build"],
            }
        ]

    def record(self, key, passed, observed, observer=None):
        assert passed, (key, observed)


def test_default_behavioral_repair_keeps_review_and_publication_available(tmp_path: Path) -> None:
    gate = RepairGate(
        GateOptions(scenario="repair-window", evidence_out=tmp_path / "evidence.json"),
        SimpleNamespace(raw_writer=None),
    )
    try:
        step_prepare(gate)
        (gate.project / "candidate.py").write_text("def compute(a, b):\n    return 0\n")
        (gate.project / "validate_candidate.py").write_text(_VALIDATOR)
        policy = gate.project / ".enginery" / "project.toml"
        # Validation intentionally passes; the independent behavioral observer drives repair.
        assert "repair_limit" not in policy.read_text()
        gate.git(gate.project, "add", ".")
        gate.git(gate.project, "commit", "-qm", "declare changed behavior")
        branch = gate.git(gate.project, "symbolic-ref", "--short", "HEAD")
        gate.git(gate.project, "push", "-q", "origin", branch)
        _, report = gate.factory("run", "owner/repo#1", "--wait-seconds", "60")
        run_id = report["run_id"]
        gate.run_id = run_id
        gate.terminal = TerminalSession()
        repaired_review = False
        for _ in range(16):
            decision = report.get("next_decision") or {}
            launches = len(gate.launches())
            if decision.get("action") == "engine review":
                if launches >= 2:
                    assert report.get("status") != "failed", report
                    assert "--repair-attempt 1" in decision["command"]
                    repaired_review = True
                path = gate.root / "REPORT.json"
                path.write_text(json.dumps({"producer": "p", "reviewer": "r", "findings": []}))
                argv = shlex.split(decision["command"].replace("REPORT.json", str(path)))
                result = gate.cli(*argv[1:])
                assert result.returncode == 0, result.stderr
            elif "pull_request.open" in json.dumps(decision):
                if repaired_review:
                    assert report.get("status") != "failed", report
                    assert "enginery review" in decision["review_command"]
                    assert "enginery decide" in decision["command"]
                    return
                executable = str(Path(sys.executable).with_name("enginery"))
                issued = gate.terminal.run(
                    [executable, "review", run_id, "--project", str(gate.project)],
                    cwd=gate.root,
                    env=gate.environment(),
                )
                assert issued.code == 0 and issued.challenge
                decided = gate.terminal.run(
                    [
                        executable,
                        "decide",
                        run_id,
                        "--project",
                        str(gate.project),
                        "--action",
                        "pull_request.open",
                        "--subject-digest",
                        issued.subject or "",
                        "--challenge",
                        issued.challenge,
                    ],
                    cwd=gate.root,
                    env=gate.environment(),
                )
                assert decided.code == 0, decided.text
            assert report.get("status") != "failed", report
            result = gate.cli(
                "continue",
                run_id,
                "--wait-seconds",
                "60",
                "--project",
                str(gate.project),
                "--json",
            )
            if result.stdout.strip():
                report = json.loads(result.stdout)
            else:
                assert any(
                    reason in result.stderr
                    for reason in ("decision_", "behavioral_acceptance_failed")
                ), result.stderr
                _, report = gate.factory("status", run_id)
        raise AssertionError(report)
    finally:
        gate.cleanup()
