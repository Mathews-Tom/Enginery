"""``stage1 build-request`` pins and the receipt/hold diagnostics of ``watch`` and ``evidence``."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import PullRequestPort, WorkLedgerPort
from enginery.cli.main import main
from enginery.cli.stage1 import _harness_for
from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError, MissingPrerequisiteError
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import (
    Stage1RunRequest,
    Stage1RunService,
    stage1_request_from_state,
)
from tests.workflows.test_stage1_runtime import RecordingPullRequests, TerminalWorkLedger

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
    assert not selection.strict_effective
    assert selection.harness_source is not None and selection.harness_source.value == "run"
    assert selection.catalog_digest == load_execution_catalog().digest


@pytest.mark.parametrize(("harness", "model"), [("omp", OMP_MODEL), ("claude-code", CLAUDE_MODEL)])
def test_strict_effective_is_accepted_as_a_modifier_of_a_pinned_model(
    tmp_path: Path, harness: str, model: str
) -> None:
    output = tmp_path / "request.json"

    exit_code = main(
        _build_argv(
            tmp_path,
            "--harness-provider",
            harness,
            "--model",
            model,
            "--strict-effective",
            output=output,
        )
    )

    assert exit_code == 0
    selection = stage1_request_from_state(
        json.loads(output.read_text(encoding="utf-8"))
    ).execution_selection
    assert selection.strict_effective and selection.selected_effort is None


@pytest.mark.parametrize(
    ("flags", "harness", "source"),
    [
        ((), "omp", "factory"),
        (("--harness-provider", "omp"), "omp", "run"),
        (("--harness-provider", "claude-code"), "claude-code", "run"),
    ],
)
def test_the_recorded_harness_source_says_whether_it_was_defaulted_or_chosen(
    tmp_path: Path, flags: tuple[str, ...], harness: str, source: str
) -> None:
    output = tmp_path / "request.json"

    assert main(_build_argv(tmp_path, *flags, output=output)) == 0

    request = stage1_request_from_state(json.loads(output.read_text(encoding="utf-8")))
    assert request.execution_selection.harness == harness
    assert request.execution_selection.harness_source is not None
    assert request.execution_selection.harness_source.value == source
    assert request.execution_configuration.harness_provider == harness
    assert request.execution_configuration.harness_executable == (
        "claude" if harness == "claude-code" else "omp"
    )


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
        (
            (
                "--harness-provider",
                "omp",
                "--model",
                OMP_MODEL,
                "--effort",
                "high",
                "--strict-effective",
            ),
            "does not report its effective effort",
        ),
        (
            (
                "--harness-provider",
                "claude-code",
                "--effort",
                "high",
                "--strict-effective",
            ),
            "does not report its effective effort",
        ),
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
                str(_fake_host(tmp_path, "omp v18.10.3")),
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
    assert hold["details"]["bound_executable_version"] == "18.10.3"  # type: ignore[index]
    execution = watch["execution"]
    assert isinstance(execution, dict)
    assert execution["holds"] == [hold]
    assert execution["selected"]["model"] == OMP_MODEL  # type: ignore[index]


def test_watch_proceeds_when_the_installed_host_is_the_bound_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "request.json"
    database = tmp_path / "ledger.db"
    healthy = _fake_host(tmp_path, "omp v18.10.3")
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


# --- receipts recorded by a real adapter run against a stand-in host executable -------------

_STAND_IN = """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
with open({log!r}, "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args in (["--help"], ["--version"]):
    print({banner!r})
    raise SystemExit(0)
host, reported, fail = {host!r}, {reported!r}, {fail!r}
events = []
if host == "omp":
    events += [{{"type": "session"}}, {{"type": "agent_start"}}]
    for identifier in reported:
        provider, _, model = identifier.partition("/")
        events.append({{"type": "message_end", "message": {{
            "role": "assistant", "provider": provider, "model": model}}}})
    events.append({{"type": "agent_end"}})
else:
    events.append({{"type": "system", "subtype": "init"}})
    for identifier in reported:
        events.append({{"type": "assistant", "message": {{"model": identifier}}}})
    events.append({{"type": "result", "subtype": "error" if fail else "success",
                    "is_error": fail}})
for event in events:
    print(json.dumps(event))
raise SystemExit(1 if fail and host == "omp" else 0)
"""
_BANNERS = {"omp": "omp v18.10.3", "claude-code": "2.1.296 (Claude Code)"}
_PINS = {"omp": OMP_MODEL, "claude-code": CLAUDE_MODEL}
_EFFORT_FLAGS = {"omp": "--thinking", "claude-code": "--effort"}
_NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=60)
_LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)


class _Run:
    """A started run whose host is a stand-in executable that records every invocation."""

    def __init__(
        self,
        tmp_path: Path,
        host: str,
        *flags: str,
        reported: tuple[str, ...] = (),
        fail: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.host = host
        self.log = tmp_path / "invocations.log"
        executable = tmp_path / "stand-in"
        executable.write_text(
            _STAND_IN.format(
                log=str(self.log), banner=_BANNERS[host], host=host, reported=reported, fail=fail
            ),
            encoding="utf-8",
        )
        executable.chmod(0o755)
        self.request_path = tmp_path / "request.json"
        self.database = tmp_path / "ledger.db"
        argv = _build_argv(
            tmp_path,
            "--harness-provider",
            host,
            "--harness-executable",
            str(executable),
            *flags,
            output=self.request_path,
        )
        assert main(argv) == 0
        assert (
            main(
                [
                    "stage1",
                    "start",
                    "--database",
                    str(self.database),
                    "--owner",
                    "op",
                    "--request",
                    str(self.request_path),
                ]
            )
            == 0
        )

    def launches(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in records if r not in (["--help"], ["--version"])]

    def service(self, ledger: LedgerService) -> tuple[Stage1RunService, Stage1RunRequest]:
        request = stage1_request_from_state(json.loads(self.request_path.read_text()))
        return (
            Stage1RunService(
                runtime=CoordinatorRuntime(ledger, owner="op"),
                ledger=ledger,
                work_ledger=cast(WorkLedgerPort, TerminalWorkLedger(request.work_snapshot)),
                pull_requests=cast(PullRequestPort, RecordingPullRequests()),
                harness=_harness_for(request.execution_configuration),
            ),
            request,
        )

    def execute(self) -> None:
        """Qualify, dispatch the real supervised worker, and collect its result."""
        ledger = LedgerService.open(self.database)
        try:
            service, request = self.service(ledger)
            service.qualify(
                request,
                external_reference=request.work_snapshot.work_item.external_reference,
                applicable_criteria=request.applicable_criteria,
                now=_NOW,
                heartbeat_window=_WINDOW,
            )
            dispatch = service.dispatch_implementation(
                request, now=_NOW, heartbeat_window=_WINDOW, lease_window=_WINDOW, limits=_LIMITS
            )
            deadline = time.monotonic() + 30
            while not dispatch.result_path.is_file():
                assert time.monotonic() < deadline, "supervised worker retained no result"
                time.sleep(0.02)
            while True:
                try:
                    service.collect_implementation(
                        request, now=_NOW + timedelta(seconds=1), heartbeat_window=_WINDOW
                    )
                    return
                except ExternalConflictError:
                    assert time.monotonic() < deadline, "worker exit never observed"
                    time.sleep(0.02)
        finally:
            ledger.close()

    def command(self, name: str) -> None:
        argv = ["stage1", name, "--database", str(self.database), "--owner", "op"]
        assert main([*argv, "--run-id", "run-1"]) == 0


def _json_of(capsys: pytest.CaptureFixture[str], run: _Run, name: str) -> dict[str, object]:
    capsys.readouterr()
    run.command(name)
    return cast(dict[str, object], json.loads(capsys.readouterr().out.strip().splitlines()[-1]))


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_pinned_run_reports_submitted_tokens_the_observed_model_and_an_unknown_effort(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    pin = _PINS[host]
    run = _Run(tmp_path, host, "--model", pin, "--effort", "high", reported=(pin,))
    run.execute()

    execution = _json_of(capsys, run, "evidence")["execution"]

    assert isinstance(execution, dict)
    assert execution["submitted"] == ["--model", pin, _EFFORT_FLAGS[host], "high"]
    effective = execution["effective"]
    assert isinstance(effective, dict)
    assert effective["model"]["state"] == "observed"  # type: ignore[index]
    assert effective["model"]["value"] == pin  # type: ignore[index]
    assert effective["effort"]["state"] == "unknown"  # type: ignore[index]
    assert effective["effort"]["value"] is None  # type: ignore[index]
    assert "no effective-effort evidence" in effective["effort"]["source"]  # type: ignore[index]
    assert execution["requested"]["effort"]["value"] == "high"  # type: ignore[index]
    assert execution["holds"] == []
    assert len(run.launches()) == 1


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_an_unreported_effective_model_is_unknown_with_its_reason_never_the_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    run = _Run(tmp_path, host, "--model", _PINS[host], reported=())
    run.execute()

    effective = _json_of(capsys, run, "evidence")["execution"]["effective"]  # type: ignore[index]

    assert effective["model"]["state"] == "unknown"
    assert effective["model"]["value"] is None
    assert "no " in effective["model"]["source"]


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_strict_run_with_an_unreported_model_shows_the_recorded_hold_everywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    run = _Run(tmp_path, host, "--model", _PINS[host], "--strict-effective", reported=())
    run.execute()

    evidence = _json_of(capsys, run, "evidence")
    watch = _json_of(capsys, run, "watch")

    for payload in (evidence, watch):
        hold = payload["hold"]
        assert isinstance(hold, dict)
        assert hold["reason"] == "effective_control_unverified"
        assert payload["execution"]["holds"] == [hold]  # type: ignore[index]
    assert watch["next_action"] == "hold"


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_failed_pinned_run_keeps_its_original_pins_and_is_never_retried_differently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    pin = _PINS[host]
    run = _Run(tmp_path, host, "--model", pin, "--effort", "high", fail=True, reported=(pin,))
    before = _json_of(capsys, run, "evidence")["execution"]
    run.execute()

    evidence = _json_of(capsys, run, "evidence")
    watch = _json_of(capsys, run, "watch")
    execution = evidence["execution"]

    assert isinstance(execution, dict) and isinstance(before, dict)
    nodes = {node["node_id"]: node for node in evidence["nodes"]}  # type: ignore[attr-defined]
    assert nodes["implement"]["status"] == "failed"
    assert watch["next_action"] == "await_human_review"
    assert execution["requested"] == before["requested"]
    assert execution["selected"] == {"model": pin, "effort": "high"}
    assert execution["selection_digest"] == before["selection_digest"]
    assert execution["submitted"] == ["--model", pin, _EFFORT_FLAGS[host], "high"]
    assert len(run.launches()) == 1
    ledger = LedgerService.open(run.database)
    try:
        service, request = run.service(ledger)
        with pytest.raises((ExternalConflictError, MissingPrerequisiteError)):
            service.dispatch_implementation(
                request, now=_NOW, heartbeat_window=_WINDOW, lease_window=_WINDOW, limits=_LIMITS
            )
    finally:
        ledger.close()
    assert len(run.launches()) == 1
