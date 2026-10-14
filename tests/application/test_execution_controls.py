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
from enginery.domain.execution_receipt import EffectiveState, ExecutionReceipt, HostObservation
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


def _in_range_host() -> InstalledHost:
    return InstalledHost("18.11.0", "json-protocol=3")


def test_a_receipt_with_no_launched_command_records_no_submitted_arguments() -> None:
    receipt = build_receipt(
        selection_for("omp", model="openai/gpt-5.2"),
        harness="omp",
        catalog=CATALOG,
        launched_command=None,
        models=(),
        probe_installed_host=_in_range_host,
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


@pytest.mark.parametrize(
    "banner",
    [
        "omp v 18.11.0\n",
        "omp v  18.11.0\n",
        " omp v18.11.0\n",
        "\nomp v18.11.0\n",
        "omp v18.11.0 \n",
        "omp v18.11.0 beta\n",
        "omp  v18.11.0\n",
        "OMP v18.11.0\n",
        "omp/18.11.0\n",
        "omp v\t18.11.0\n",
    ],
)
def test_the_omp_probe_yields_a_version_only_for_the_exact_banner(banner: str) -> None:
    assert omp.probe_installed_host("omp", _runner(banner)) is None


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\n\nUSAGE\n", ""])
def test_the_omp_probe_accepts_the_exact_banner_with_any_line_ending(ending: str) -> None:
    assert omp.probe_installed_host("omp", _runner(f"omp v18.11.0{ending}")) == InstalledHost(
        "18.11.0", "json-protocol=3"
    )


@pytest.mark.parametrize(
    "banner",
    [
        " 2.1.300 (Claude Code)\n",
        "2.1.300  (Claude Code)\n",
        "2.1.300 (Claude Code) \n",
        "2.1.300 (claude code)\n",
        "2.1.300\n",
        "(Claude Code)\n",
        "  (Claude Code)\n",
        "\n2.1.300 (Claude Code)\n",
        "Claude Code 2.1.300\n",
        "2.1.300 extra (Claude Code)\n",
    ],
)
def test_the_claude_probe_yields_a_version_only_for_the_exact_banner(banner: str) -> None:
    assert claude_code.probe_installed_host("claude", _runner(banner)) is None


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_the_claude_probe_accepts_the_exact_banner_with_any_line_ending(ending: str) -> None:
    assert claude_code.probe_installed_host(
        "claude", _runner(f"2.1.300 (Claude Code){ending}")
    ) == InstalledHost("2.1.300", "stream-json-schema=1")


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
        probe_installed_host=_in_range_host,
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
            probe_installed_host=_in_range_host,
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
        probe_installed_host=_in_range_host,
    )

    assert receipt.effective_model.state is EffectiveState.UNKNOWN
    assert receipt.effective_model.value is None
    assert "no effective-model evidence" in receipt.effective_model.source


def test_a_range_bound_receipt_records_the_range_the_tested_and_the_observed_version() -> None:
    receipt = build_receipt(
        selection_for("omp", model="openai/gpt-5.2"),
        harness="omp",
        catalog=CATALOG,
        launched_command=None,
        models=(),
        probe_installed_host=_in_range_host,
    )

    assert receipt.host == HostObservation(
        accepted_executable_versions=">=18.10.3,<19.0.0",
        tested_executable_version="18.10.3",
        observed_executable_version="18.11.0",
    )
    assert ExecutionReceipt.from_state(receipt.to_state()) == receipt


def test_an_exact_bound_receipt_records_no_range_and_still_the_observed_version() -> None:
    receipt = build_receipt(
        selection_for("omp", model="openai/gpt-5.2", catalog=load_execution_catalog("2")),
        harness="omp",
        catalog=load_execution_catalog("2"),
        launched_command=None,
        models=(),
        probe_installed_host=_in_range_host,
    )

    assert receipt.host == HostObservation(
        accepted_executable_versions=None,
        tested_executable_version="18.10.3",
        observed_executable_version="18.11.0",
    )


def test_an_unobservable_host_is_recorded_as_no_observation_not_as_the_tested_version() -> None:
    receipt = build_receipt(
        selection_for("omp", model="openai/gpt-5.2"),
        harness="omp",
        catalog=CATALOG,
        launched_command=None,
        models=(),
        probe_installed_host=lambda: None,
    )

    assert receipt.host is not None
    assert receipt.host.observed_executable_version is None
    assert receipt.host.tested_executable_version == "18.10.3"


def test_a_selection_without_native_controls_never_probes_for_the_receipt() -> None:
    def forbidden() -> InstalledHost | None:
        raise AssertionError("a host-default run must not probe the host")

    receipt = build_receipt(
        selection_for("omp"),
        harness="omp",
        catalog=CATALOG,
        launched_command=None,
        models=(),
        probe_installed_host=forbidden,
    )

    assert receipt.host is None


def test_a_receipt_recorded_before_host_versions_decodes_without_a_host() -> None:
    receipt = build_receipt(
        selection_for("omp"),
        harness="omp",
        catalog=CATALOG,
        launched_command=None,
        models=(),
        probe_installed_host=_in_range_host,
    )
    state = receipt.to_state()
    del state["host"]

    assert ExecutionReceipt.from_state(state).host is None
