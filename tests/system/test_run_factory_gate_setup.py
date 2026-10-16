"""The ``setup`` scenario of the factory gate: local only, real CLI, no hosted session.

The scenario drives the real ``enginery`` CLI in subprocesses against a disposable home with
recording ``gh`` and ``omp`` stand-ins, so these tests need no engine, no network and no
credential. They prove the scenario exits 0 only when every required assertion ran and
passed, that a step that fails is never counted as a pass, and that the flags that belong to
hosted or container scenarios are refused for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import factory_gate_setup
from factory_gate_evidence import EXIT_FAIL, EXIT_HOLD, EXIT_PASS, validate_evidence
from run_factory_gate import main, parse_options

EVIDENCE = "evidence.json"


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["--scenario", "setup", "--evidence-out", str(tmp_path / EVIDENCE), *extra]


def _run(tmp_path: Path) -> tuple[int, dict[str, Any]]:
    code = main(_argv(tmp_path))
    return code, json.loads((tmp_path / EVIDENCE).read_text(encoding="utf-8"))


def test_the_setup_scenario_passes_only_with_every_required_assertion_run_and_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, document = _run(tmp_path)

    assert code == EXIT_PASS
    assert validate_evidence(document) == []
    assert document["verdict"] == "pass" and document["mode"] == "local"
    recorded = {item["id"]: item for item in document["assertions"]}
    assert set(recorded) == set(factory_gate_setup.REQUIRED)
    assert all(item["ran"] is True and item["status"] == "pass" for item in recorded.values())
    assert all(item["commands"] and item["observer"] for item in recorded.values())
    assert document["external_effect_counts"]["hosted_inference_calls"] == 0
    assert document["external_effect_counts"]["hosted_sessions"] == 0
    assert document["external_effect_counts"]["global_location_changes"] == 0
    assert document["external_effect_counts"]["harness_launches_recorded"] == 3
    assert (tmp_path / "evidence.raw").is_dir()
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    for identity in ("subject", "build", "config", "host", "evaluator"):
        assert document["identities"][identity]


def test_a_layer_that_cannot_be_submitted_is_a_failure_not_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> dict[str, Any]:
        raise RuntimeError("the adapter never submitted a worker")

    monkeypatch.setattr(factory_gate_setup, "_submit", refuse)

    code, document = _run(tmp_path)

    assert code == EXIT_FAIL
    recorded = {item["id"]: item for item in document["assertions"]}
    for key in (
        "layer-user-reaches-submission",
        "layer-project-over-user-reaches-submission",
        "layer-run-over-project-reaches-submission",
    ):
        assert recorded[key]["status"] == "fail" and recorded[key]["ran"] is False
    assert recorded["setup-preview-writes-nothing"]["status"] == "pass"
    assert recorded["no-hosted-session"]["status"] == "fail"  # fewer than three launches
    assert document["verdict"] == "fail"


def test_a_missing_git_is_a_hold_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(factory_gate_setup.shutil, "which", lambda *args, **kwargs: None)

    code, document = _run(tmp_path)

    assert code == EXIT_HOLD
    assert document["verdict"] == "hold"
    assert all(item["ran"] is False for item in document["assertions"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--live"],
        ["--harness", "omp"],
        ["--model", "openai/gpt-5"],
        ["--confirm-single-session"],
        ["--session-deadline-seconds", "900"],
        ["--session-deadline-seconds=120"],
        ["--session-deadline", "120"],
        ["--session-dead=120"],
        ["--custody-receipt", "receipt.json"],
        ["--gateway-url", "http://127.0.0.1:1", "--gateway-token-file", "token"],
        ["--docker-context", "colima"],
        ["--runner-image", "sha256:" + "a" * 64],
    ],
)
def test_flags_for_hosted_or_container_scenarios_are_refused(
    tmp_path: Path, extra: list[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_options(_argv(tmp_path, *extra))

    assert refused.value.code == 2


def test_setup_preserves_inherited_git_configuration_without_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    for index, (key, value) in enumerate(
        (("maintenance.auto", "true"), ("gc.auto", "1"), ("test.inherited", "preserved"))
    ):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    original = factory_gate_setup.SetupGate.new_project

    def new_project(gate: factory_gate_setup.SetupGate, name: str) -> Path:
        project = original(gate, name)
        assert gate.git(project, "config", "maintenance.auto") == "false"
        assert gate.git(project, "config", "gc.auto") == "0"
        assert gate.git(project, "config", "test.inherited") == "preserved"
        return project

    monkeypatch.setattr(factory_gate_setup.SetupGate, "new_project", new_project)
    code, document = _run(tmp_path)

    assert code == EXIT_PASS
    assert document["verdict"] == "pass"


def test_setup_apply_rejects_an_unpreviewed_created_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = factory_gate_setup.SetupGate.cli

    def cli(gate: factory_gate_setup.SetupGate, *arguments: str, **kwargs: Any) -> Any:
        process = original(gate, *arguments, **kwargs)
        if arguments[:2] == ("factory", "init") and "--apply" in arguments:
            project = Path(arguments[arguments.index("--project") + 1])
            (project / "unpreviewed").write_text("unexpected\n", encoding="utf-8")
        return process

    monkeypatch.setattr(factory_gate_setup.SetupGate, "cli", cli)
    code, document = _run(tmp_path)

    assert code == EXIT_FAIL
    recorded = {item["id"]: item for item in document["assertions"]}
    assert recorded["setup-apply-performs-previewed-plan"]["status"] == "fail"


def test_setup_apply_rejects_changes_to_an_existing_project_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = factory_gate_setup.SetupGate.cli

    def cli(gate: factory_gate_setup.SetupGate, *arguments: str, **kwargs: Any) -> Any:
        process = original(gate, *arguments, **kwargs)
        if arguments[:2] == ("factory", "init") and "--apply" in arguments:
            project = Path(arguments[arguments.index("--project") + 1])
            (project / "README").write_text("unexpected replacement\n", encoding="utf-8")
        return process

    monkeypatch.setattr(factory_gate_setup.SetupGate, "cli", cli)
    code, document = _run(tmp_path)

    assert code == EXIT_FAIL
    recorded = {item["id"]: item for item in document["assertions"]}
    assert recorded["setup-apply-performs-previewed-plan"]["status"] == "fail"
