"""Manual-path gate: real isolated CLI journey, complete evidence and local-only authority."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import factory_gate_manual_path as manual_path
from factory_gate_evidence import EXIT_FAIL, EXIT_PASS, validate_evidence
from run_factory_gate import main, parse_options


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["--scenario", "manual-path", "--evidence-out", str(tmp_path / "evidence.json"), *extra]


def _document(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8")))


def test_manual_path_exercises_every_recovery_and_authority_assertion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_argv(tmp_path))
    document = _document(tmp_path)

    assert code == EXIT_PASS, document.get("details")
    assert validate_evidence(document) == []
    assert document["verdict"] == "pass" and document["mode"] == "local"
    assertions = {item["id"]: item for item in document["assertions"]}
    assert set(assertions) == set(manual_path.REQUIRED)
    assert all(item["ran"] and item["status"] == "pass" for item in assertions.values())
    assert all(
        item["commands"] and item["observer"] and item["raw_artifacts"]
        for item in assertions.values()
    )
    client = assertions["client-kill-worker-survives"]["observed"]
    coordinator = assertions["coordinator-kill-after-pr"]["observed"]
    assert client["killed"]["exit_code"] == coordinator["killed"]["exit_code"] == -9
    assert client["status"]["worker"]["liveness"] == "alive"
    assert assertions["coordinator-kill-after-pr"]["observer"]["response_still_blocked"]
    recovered = assertions["coordinator-resume-single-pr"]
    assert len(recovered["observer"]["pr_posts"]) == 1
    assert recovered["observed"]["status"]["pull_request"]["number"] == 17
    retained = assertions["retained-workspace-hold-repair"]["observed"]
    assert retained["exit"] == 5
    assert retained["json"]["blocked_workspace"]["status"] == "retained"
    assert any(hold["prerequisite"] == "retained workspace" for hold in retained["json"]["holds"])
    findings = assertions["terminal-review-findings-only"]["observed"]["json"]
    assert findings["findings_recorded"] is True
    publication = assertions["terminal-publication-decision"]
    assert publication["observer"]["principal_id"] == manual_path.OPERATOR_PRINCIPAL
    assert "pull_request.open" in publication["observed"]["review"]
    assert "<challenge-sha256:" in publication["observed"]["review"]
    drain = assertions["drain-releases-resources"]["observed"]
    assert drain["wait"]["released"] == "by resume/reconcile"
    assert drain["stop"]["released"] == "now" and drain["stop"]["remaining"] == []
    effects = document["external_effect_counts"]
    assert effects["pull_request_creations"] == 1
    assert effects["harness_launches_recorded"] == 2
    assert effects["issue_comments"] == effects["issue_lifecycle_comments"] == 1
    terminal = assertions["verified-open-pr-outcome"]["observed"]["json"]
    assert terminal["workflow_complete"] and terminal["terminal_decision"] == "merge_ready"
    assert terminal["terminal_evidence"]["verify_status"] == "passed"
    assert terminal["terminal_evidence"]["observation_registered"]
    assert effects["github_reads"] > 0
    for name in (
        "hosted_inference_calls",
        "issue_label_mutations",
        "merges",
        "releases",
        "deployments",
        "global_location_changes",
    ):
        assert effects[name] == 0
    for identity in ("subject", "build", "config", "host", "evaluator"):
        assert document["identities"][identity]
    assert document["identities"]["engine"] is None
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    raw = tmp_path / "evidence.raw"
    for name in (
        "commands.json",
        "gh-calls.json",
        "harness-launches.json",
        "container-engine-calls.json",
        "pull-request.json",
        "request.json",
    ):
        assert (raw / name).is_file()
    engine_calls = json.loads((raw / "container-engine-calls.json").read_text(encoding="utf-8"))
    launches = json.loads((raw / "harness-launches.json").read_text(encoding="utf-8"))
    assert effects["container_engine_calls"] == len(engine_calls) == 0
    assert (
        effects["hosted_sessions"]
        == sum(bool(launch["hosted_session_requested"]) for launch in launches)
        == 0
    )
    commands = json.loads((raw / "commands.json").read_text(encoding="utf-8"))
    assert all(
        "exit_code" in command and "stdout" in command and "stderr" in command
        for command in commands
    )


def test_unexercised_required_assertions_fail_instead_of_silently_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_journey(gate: manual_path.ManualPathGate) -> None:
        return None

    monkeypatch.setattr(manual_path, "run", no_journey)
    code = main(_argv(tmp_path))
    document = _document(tmp_path)

    assert code == EXIT_FAIL and document["verdict"] == "fail"
    assert {item["id"] for item in document["assertions"]} == set(manual_path.REQUIRED)
    assert all(item["ran"] is False and item["status"] == "fail" for item in document["assertions"])


def test_failed_prerequisite_records_all_unreachable_assertions_as_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(gate: manual_path.ManualPathGate) -> None:
        raise RuntimeError("project setup deliberately refused")

    monkeypatch.setattr(manual_path, "step_prepare", refuse)
    code = main(_argv(tmp_path))
    document = _document(tmp_path)
    assertions = {item["id"]: item for item in document["assertions"]}

    assert code == EXIT_FAIL
    assert set(assertions) == set(manual_path.REQUIRED)
    for key in (
        "run-bound-dispatch",
        "client-kill-worker-survives",
        "coordinator-kill-after-pr",
        "coordinator-resume-single-pr",
        "drain-releases-resources",
    ):
        assert assertions[key]["ran"] is False
        assert assertions[key]["status"] == "fail"
    assert assertions["local-only-effect-counts"]["status"] == "fail"


@pytest.mark.parametrize(
    "extra",
    [
        ["--live"],
        ["--harness", "omp"],
        ["--harness", "claude"],
        ["--model", "example/model"],
        ["--confirm-single-session"],
        ["--custody-receipt", "receipt.json"],
        ["--gateway-url", "http://127.0.0.1:1"],
        ["--gateway-token-file", "token"],
        ["--docker-context", "fixture"],
        ["--runner-image", "sha256:" + "a" * 64],
        ["--relay-image", "sha256:" + "a" * 64],
        ["--session-deadline-seconds", "120"],
        ["--session-deadline-seconds=120"],
        ["--session-dead=120"],
    ],
)
def test_manual_path_rejects_hosted_engine_and_custody_flags(
    tmp_path: Path, extra: list[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_options(_argv(tmp_path, *extra))

    assert refused.value.code == 2
    assert not (tmp_path / "evidence.json").exists()


@pytest.mark.parametrize(
    ("code", "stderr"),
    [
        (1, "decision_required: unrelated failure"),
        (5, "runner_unavailable: not an authority hold"),
        (5, ""),
    ],
)
def test_publication_hold_does_not_suppress_unrelated_cli_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, stderr: str,
) -> None:
    gate = object.__new__(manual_path.ManualPathGate)
    gate.project = tmp_path
    gate.run_id = "held-run"

    def refuse(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(arguments), code, stdout="", stderr=stderr)

    monkeypatch.setattr(gate, "cli", refuse)
    with pytest.raises(RuntimeError, match="expected publication hold decision_required"):
        gate.publication_hold()
