from __future__ import annotations

import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.execution import selection_for

from enginery.adapters import claude_code, omp
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.execution_controls import (
    build_receipt,
    expected_native_arguments,
    extract_native_arguments,
)
from enginery.application.work_ports import HarnessTask
from enginery.domain.execution_catalog import ExecutionCatalog, InstalledHost
from enginery.domain.execution_receipt import EffectiveState
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.ledger.artifact_store import ArtifactStore

CATALOG = load_execution_catalog()
OMP = CATALOG.host("omp")


def test_extraction_reads_control_pairs_from_the_command_and_skips_the_prompt() -> None:
    command = (
        "omp",
        "--mode=json",
        "--model",
        "openai/gpt-5.2",
        "--thinking",
        "high",
        "-p",
        "mention --model nonsense and --fallback-model x in the prompt",
    )

    assert extract_native_arguments(command, host=OMP) == (
        "--model",
        "openai/gpt-5.2",
        "--thinking",
        "high",
    )


def test_extraction_reports_steering_flags_the_selection_never_asked_for() -> None:
    command = ("omp", "--fallback-model", "other", "--model=x/y", "--model")

    assert extract_native_arguments(command, host=OMP) == (
        "--fallback-model",
        "other",
        "--model=x/y",
        "--model",
        "<missing>",
    )


def test_expected_arguments_follow_the_selection_and_are_empty_for_a_host_default() -> None:
    pinned = selection_for("omp", model="openai/gpt-5.2", effort="low")

    assert expected_native_arguments(pinned, catalog=CATALOG) == (
        "--model",
        "openai/gpt-5.2",
        "--thinking",
        "low",
    )
    assert expected_native_arguments(selection_for("omp"), catalog=CATALOG) == ()


def test_a_receipt_with_no_launched_command_records_no_submitted_arguments() -> None:
    receipt = build_receipt(
        selection_for("omp", model="openai/gpt-5.2"),
        harness="omp",
        catalog=CATALOG,
        launched_command=None,
        models=(),
    )

    assert receipt.submitted_arguments == ()
    assert receipt.effective_model.state is EffectiveState.UNKNOWN


def _runner(stdout: str, code: int = 0):  # type: ignore[no-untyped-def]
    return lambda arguments: subprocess.CompletedProcess(arguments, code, stdout=stdout, stderr="")


def _missing(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    raise PermissionError(arguments[0])


def test_the_omp_probe_observes_the_version_and_declares_the_protocol() -> None:
    assert omp.probe_installed_host("omp", _runner("omp v18.10.2\n\nUSAGE\n")) == InstalledHost(
        "18.10.2", "json-protocol=3"
    )


@pytest.mark.parametrize(
    "runner",
    [_runner("not omp"), _runner("omp v18.10.2", 1), _runner("omp v\n"), _missing],
    ids=["wrong-banner", "failing", "blank-version", "unrunnable"],
)
def test_the_omp_probe_reports_an_unidentifiable_host_as_unobservable(runner: object) -> None:
    assert omp.probe_installed_host("omp", runner) is None  # type: ignore[arg-type]


def test_the_claude_probe_observes_the_version_and_declares_the_protocol() -> None:
    assert claude_code.probe_installed_host(
        "claude", _runner("2.1.296 (Claude Code)\n")
    ) == InstalledHost("2.1.296", "stream-json-schema=1")


@pytest.mark.parametrize(
    "runner",
    [_runner("something else"), _runner("2.1.296 (Claude Code)", 1), _missing],
    ids=["wrong-banner", "failing", "unrunnable"],
)
def test_the_claude_probe_reports_an_unidentifiable_host_as_unobservable(runner: object) -> None:
    assert claude_code.probe_installed_host("claude", runner) is None  # type: ignore[arg-type]


def _task_for(harness_name: str, operation_id: str, *, model: str | None) -> HarnessTask:
    return HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId(operation_id),
        workspace_path=Path("/tmp/workspace"),
        objective="Change one bounded behavior.",
        acceptance_criteria=("tests pass",),
        constraints=("none",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=Decimal("1.00"),
        selection=selection_for(harness_name, model=model),
    )


@pytest.mark.parametrize(
    ("harness_name", "pin", "banner"),
    [
        ("omp", "openai/gpt-5.2", "omp v18.10.3\n"),
        ("claude-code", "claude-opus-4-8", "2.1.296 (Claude Code)\n"),
    ],
)
def test_an_operation_id_spelled_like_a_flag_is_not_read_as_a_launched_control(
    tmp_path: Path, harness_name: str, pin: str, banner: str
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    config_kwargs: dict[str, object] = {"credential_reference": "profile"}
    if harness_name == "omp":
        harness: omp.OmpHarness | claude_code.ClaudeCodeHarness = omp.OmpHarness(
            omp.OmpAdapterConfig(**config_kwargs),  # type: ignore[arg-type]
            store,
            command_runner=_runner(banner),
        )
    else:
        harness = claude_code.ClaudeCodeHarness(
            claude_code.ClaudeCodeAdapterConfig(**config_kwargs),  # type: ignore[arg-type]
            store,
            command_runner=_runner(banner),
        )
    host = CATALOG.host(harness_name)
    result_path = tmp_path / "result.json"

    hostile = harness.supervised_command(
        _task_for(harness_name, "--model", model=pin), result_path=result_path
    )
    default = harness.supervised_command(
        _task_for(harness_name, "--model", model=None), result_path=result_path
    )

    assert "--model" in hostile[: hostile.index("--")]
    assert extract_native_arguments(hostile, host=host) == ("--model", pin)
    assert extract_native_arguments(default, host=host) == ()


def _catalog_with_model_evidence(evidence: str) -> ExecutionCatalog:
    return replace(
        CATALOG,
        hosts=tuple(
            replace(host, model=replace(host.model, effective_evidence=evidence))
            for host in CATALOG.hosts
        ),
    )


@pytest.mark.parametrize("harness_name", ["omp", "claude-code"])
def test_the_receipt_source_is_exactly_the_catalogs_effective_model_evidence(
    harness_name: str,
) -> None:
    pin = "openai/gpt-5.2" if harness_name == "omp" else "claude-opus-4-8"

    receipt = build_receipt(
        selection_for(harness_name, model=pin),
        harness=harness_name,
        catalog=CATALOG,
        launched_command=None,
        models=(pin,),
    )

    assert receipt.effective_model.state is EffectiveState.OBSERVED
    assert receipt.effective_model.source == CATALOG.host(harness_name).model.effective_evidence
    custom = _catalog_with_model_evidence("a custom evidence description")
    assert (
        build_receipt(
            selection_for(harness_name, model=pin),
            harness=harness_name,
            catalog=custom,
            launched_command=None,
            models=(pin,),
        ).effective_model.source
        == "a custom evidence description"
    )


@pytest.mark.parametrize("harness_name", ["omp", "claude-code"])
def test_a_catalog_without_effective_model_evidence_leaves_the_effective_model_unknown(
    harness_name: str,
) -> None:
    pin = "openai/gpt-5.2" if harness_name == "omp" else "claude-opus-4-8"

    receipt = build_receipt(
        selection_for(harness_name, model=pin),
        harness=harness_name,
        catalog=_catalog_with_model_evidence(""),
        launched_command=None,
        models=(pin,),
    )

    assert receipt.effective_model.state is EffectiveState.UNKNOWN
    assert receipt.effective_model.value is None
    assert "no effective-model evidence" in receipt.effective_model.source
