from __future__ import annotations

import subprocess

import pytest
from tests.support.execution import selection_for

from enginery.adapters import claude_code, omp
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.execution_controls import (
    build_receipt,
    expected_native_arguments,
    extract_native_arguments,
)
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.execution_receipt import EffectiveState

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
