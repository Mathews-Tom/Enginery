"""``stage1 build-request`` pins and the receipt/hold diagnostics of ``watch`` and ``evidence``."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.application.execution_catalog import load_execution_catalog
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.workflows.stage1 import stage1_request_from_state

OMP_MODEL = "openai/gpt-5.2"
CLAUDE_MODEL = "claude-opus-4-8"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _build_argv(tmp_path: Path, *extra: str, output: Path | None = None) -> list[str]:
    repository, base_revision = (
        _repository(tmp_path)
        if not (tmp_path / "repository").exists()
        else (
            tmp_path / "repository",
            _git("rev-parse", "HEAD", cwd=tmp_path / "repository"),
        )
    )
    return [
        "stage1",
        "build-request",
        "--output",
        str(output or tmp_path / "request.json"),
        "--run-id",
        "run-1",
        "--repository",
        "owner/repo",
        "--external-reference",
        "owner/repo#1",
        "--source-snapshot-reference",
        "issue:1@1",
        "--source-revision",
        "1",
        "--base-revision",
        base_revision,
        "--title",
        "Bounded change",
        "--objective",
        "Change one bounded behavior.",
        "--acceptance-criterion",
        "observable result",
        "--repository-path",
        str(repository),
        "--workspace-path",
        str(tmp_path / "workspace"),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        *extra,
    ]


def _fake_host(tmp_path: Path, banner: str) -> Path:
    executable = tmp_path / "fake-host"
    executable.write_text(f"#!/bin/sh\necho '{banner}'\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _run_json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> dict[str, object]:
    capsys.readouterr()
    assert main(argv) == 0
    parsed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize(
    ("harness", "model", "effort"),
    [("omp", OMP_MODEL, "high"), ("claude-code", CLAUDE_MODEL, "xhigh")],
)
def test_build_request_pins_an_exact_model_and_native_effort_in_the_run_layer(
    tmp_path: Path, harness: str, model: str, effort: str
) -> None:
    output = tmp_path / "request.json"

    exit_code = main(
        _build_argv(
            tmp_path,
            "--harness-provider",
            harness,
            "--model",
            model,
            "--effort",
            effort,
            "--strict-effective",
            output=output,
        )
    )

    assert exit_code == 0
    selection = stage1_request_from_state(
        json.loads(output.read_text(encoding="utf-8"))
    ).execution_selection
    assert (selection.selected_model, selection.selected_effort) == (model, effort)
    assert selection.model.pinned and selection.effort.pinned
    assert selection.model.source is not None and selection.model.source.value == "run"
    assert selection.mode.value == "manual_pin"
    assert selection.strict_effective
    assert selection.catalog_digest == load_execution_catalog().digest


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("--harness-provider", "claude-code", "--model", "opus"), "not an exact claude-code"),
        (("--harness-provider", "claude-code", "--model", "sonnet"), "not an exact claude-code"),
        (("--harness-provider", "omp", "--model", "opus"), "not an exact omp model pin"),
        (("--harness-provider", "omp", "--effort", "ultra"), "not a supported omp effort"),
        (("--harness-provider", "omp", "--effort", "auto"), "cannot be pinned"),
        (("--harness-provider", "claude-code", "--effort", "minimal"), "not a supported"),
        (("--strict-effective",), "needs at least one selected control"),
    ],
)
def test_build_request_rejects_unsupported_pins_before_writing_anything(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
    message: str,
) -> None:
    output = tmp_path / "request.json"

    exit_code = main(_build_argv(tmp_path, *flags, output=output))

    assert exit_code == 2
    assert message in capsys.readouterr().err
    assert not output.exists()


def test_evidence_reports_requested_selected_submitted_and_effective_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "request.json"
    assert (
        main(
            _build_argv(
                tmp_path,
                "--model",
                OMP_MODEL,
                "--effort",
                "high",
                "--harness-executable",
                str(_fake_host(tmp_path, "omp v18.10.2")),
                output=output,
            )
        )
        == 0
    )
    database = tmp_path / "ledger.db"
    assert (
        main(
            [
                "stage1",
                "start",
                "--database",
                str(database),
                "--owner",
                "op",
                "--request",
                str(output),
            ]
        )
        == 0
    )

    evidence = _run_json(
        capsys,
        ["stage1", "evidence", "--database", str(database), "--owner", "op", "--run-id", "run-1"],
    )

    execution = evidence["execution"]
    assert isinstance(execution, dict)
    assert execution["binding"] == "bound"
    assert execution["support_tier"] == "experimental"
    assert execution["requested"] == {
        "model": {"value": OMP_MODEL, "source": "run", "pinned": True},
        "effort": {"value": "high", "source": "run", "pinned": True},
    }
    assert execution["selected"] == {"model": OMP_MODEL, "effort": "high"}
    assert execution["submitted"] is None
    effective = execution["effective"]
    assert isinstance(effective, dict)
    assert effective["model"]["state"] == "unknown"  # type: ignore[index]
    assert effective["effort"]["value"] is None  # type: ignore[index]
    assert execution["holds"] == []


def test_a_host_default_run_is_reported_as_host_default_never_a_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "request.json"
    assert main(_build_argv(tmp_path, output=output)) == 0
    database = tmp_path / "ledger.db"
    assert (
        main(
            [
                "stage1",
                "start",
                "--database",
                str(database),
                "--owner",
                "op",
                "--request",
                str(output),
            ]
        )
        == 0
    )

    watch = _run_json(
        capsys,
        ["stage1", "watch", "--database", str(database), "--owner", "op", "--run-id", "run-1"],
    )

    execution = watch["execution"]
    assert isinstance(execution, dict)
    assert execution["mode"] == "host_default"
    assert execution["selected"] == {"model": "host default", "effort": "host default"}
    assert execution["requested"]["model"]["source"] is None  # type: ignore[index]
    assert watch["hold"] is None


def test_watch_holds_a_pinned_run_whose_installed_host_is_not_the_bound_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "request.json"
    database = tmp_path / "ledger.db"
    drifted = _fake_host(tmp_path, "omp v0.0.1")
    assert (
        main(
            _build_argv(
                tmp_path, "--model", OMP_MODEL, "--harness-executable", str(drifted), output=output
            )
        )
        == 0
    )
    assert (
        main(
            [
                "stage1",
                "start",
                "--database",
                str(database),
                "--owner",
                "op",
                "--request",
                str(output),
            ]
        )
        == 0
    )

    watch = _run_json(
        capsys,
        ["stage1", "watch", "--database", str(database), "--owner", "op", "--run-id", "run-1"],
    )

    assert watch["next_action"] == "hold"
    hold = watch["hold"]
    assert isinstance(hold, dict)
    assert hold["reason"] == "host_fingerprint_drift"
    assert hold["details"]["installed_executable_version"] == "0.0.1"  # type: ignore[index]
    assert hold["details"]["bound_executable_version"] == "18.10.2"  # type: ignore[index]
    execution = watch["execution"]
    assert isinstance(execution, dict)
    assert execution["holds"] == [hold]
    assert execution["selected"]["model"] == OMP_MODEL  # type: ignore[index]


def test_watch_proceeds_when_the_installed_host_is_the_bound_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "request.json"
    database = tmp_path / "ledger.db"
    healthy = _fake_host(tmp_path, "omp v18.10.2")
    assert (
        main(
            _build_argv(
                tmp_path, "--model", OMP_MODEL, "--harness-executable", str(healthy), output=output
            )
        )
        == 0
    )
    assert (
        main(
            [
                "stage1",
                "start",
                "--database",
                str(database),
                "--owner",
                "op",
                "--request",
                str(output),
            ]
        )
        == 0
    )

    watch = _run_json(
        capsys,
        ["stage1", "watch", "--database", str(database), "--owner", "op", "--run-id", "run-1"],
    )

    assert watch["next_action"] == "qualify"
    assert watch["hold"] is None


def test_watch_holds_a_run_bound_to_a_catalog_that_is_no_longer_current(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "request.json"
    database = tmp_path / "ledger.db"
    assert main(_build_argv(tmp_path, "--model", OMP_MODEL, output=output)) == 0
    assert (
        main(
            [
                "stage1",
                "start",
                "--database",
                str(database),
                "--owner",
                "op",
                "--request",
                str(output),
            ]
        )
        == 0
    )
    bound = _run_json(
        capsys,
        ["stage1", "evidence", "--database", str(database), "--owner", "op", "--run-id", "run-1"],
    )
    drifted = replace(load_execution_catalog(), digest=Digest.of_bytes(b"later"), version="2")
    monkeypatch.setattr("enginery.workflows.stage1.load_execution_catalog", lambda: drifted)

    watch = _run_json(
        capsys,
        ["stage1", "watch", "--database", str(database), "--owner", "op", "--run-id", "run-1"],
    )

    assert watch["next_action"] == "hold"
    hold = watch["hold"]
    assert isinstance(hold, dict) and hold["reason"] == "catalog_drift"
    execution = watch["execution"]
    assert isinstance(execution, dict)
    assert execution["selection_digest"] == bound["execution"]["selection_digest"]  # type: ignore[index]
    assert execution["catalog"]["digest"] == str(load_execution_catalog().digest)  # type: ignore[index]
