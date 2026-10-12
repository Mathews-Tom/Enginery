"""Both harness adapters submit native model and effort controls or reject before launch."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.execution import selection_for

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessTask
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.execution_receipt import EffectiveState
from enginery.domain.execution_selection import ExecutionSelection, RequestedControl
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.ledger.artifact_store import ArtifactStore


class _Process:
    pid = 4242

    def __init__(self, output: str) -> None:
        self.output = output
        self.returncode: int | None = 0

    def communicate(self) -> tuple[str, str | None]:
        return self.output, None

    def poll(self) -> int | None:
        return self.returncode


def _omp_output(*, with_model: bool) -> str:
    records: list[dict[str, object]] = [{"type": "session", "version": 3}, {"type": "agent_start"}]
    if with_model:
        records.append(
            {
                "type": "message_end",
                "message": {"role": "assistant", "provider": "openai", "model": "gpt-5.2"},
            }
        )
    records.append({"type": "agent_end"})
    return "\n".join(json.dumps(record) for record in records)


def _claude_output(*, with_model: bool) -> str:
    records: list[dict[str, object]] = [{"type": "system", "subtype": "init"}]
    if with_model:
        records.append({"type": "assistant", "message": {"model": "claude-opus-4-8"}})
    records.append({"type": "result", "subtype": "success", "is_error": False})
    return "\n".join(json.dumps(record) for record in records)


class _Rig:
    def __init__(self, tmp_path: Path, host: str, *, installed: str | None, output: str) -> None:
        self.tmp_path = tmp_path
        self.probes: list[tuple[str, ...]] = []
        self.launches: list[tuple[str, ...]] = []
        self.installed = installed
        self.output = output
        kwargs: dict[str, object] = {
            "command_runner": self._runner,
            "process_factory": self._factory,
        }
        store = ArtifactStore(tmp_path / "artifacts")
        if host == "omp":
            self.harness: OmpHarness | ClaudeCodeHarness = OmpHarness(
                OmpAdapterConfig(credential_reference="profile"),
                store,
                **kwargs,  # type: ignore[arg-type]
            )
        else:
            self.harness = ClaudeCodeHarness(
                ClaudeCodeAdapterConfig(credential_reference="profile"),
                store,
                **kwargs,  # type: ignore[arg-type]
            )
        self.host = host

    def _runner(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.probes.append(arguments)
        if self.installed is None:
            raise FileNotFoundError(arguments[0])
        return subprocess.CompletedProcess(arguments, 0, stdout=self.installed, stderr="")

    def _factory(self, arguments: tuple[str, ...], workspace: Path) -> _Process:
        self.launches.append(arguments)
        return _Process(self.output)


_OMP_INSTALLED = "omp v18.10.2\n\nUSAGE\n"
_CLAUDE_INSTALLED = "2.1.296 (Claude Code)\n"
_HOSTS = {
    "omp": ("openai/gpt-5.2", "--thinking", _OMP_INSTALLED, "omp v18.10.3\n"),
    "claude-code": ("claude-opus-4-8", "--effort", _CLAUDE_INSTALLED, "2.1.297 (Claude Code)\n"),
}


def _task(workspace: Path, selection: ExecutionSelection) -> HarnessTask:
    return HarnessTask(
        run_id=RunId("run-1"),
        node_id=NodeId("implement"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId("controls-1"),
        workspace_path=workspace,
        objective="Implement the requested provider behavior.",
        acceptance_criteria=("tests pass",),
        constraints=("none",),
        permitted_capabilities=("repository-write",),
        evidence_requirements=("test report",),
        time_budget_seconds=60,
        cost_budget=Decimal("1.00"),
        selection=selection,
    )


def _contains(command: tuple[str, ...], tokens: tuple[str, ...]) -> bool:
    return any(command[i : i + len(tokens)] == tokens for i in range(len(command)))


def _rig(
    tmp_path: Path, host: str, *, installed: str | None = None, with_model: bool = True
) -> _Rig:
    output = (
        _omp_output(with_model=with_model)
        if host == "omp"
        else _claude_output(with_model=with_model)
    )
    return _Rig(tmp_path, host, installed=installed or _HOSTS[host][2], output=output)


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_host_default_selection_launches_without_control_flags_or_a_probe(
    tmp_path: Path, host: str
) -> None:
    rig = _rig(tmp_path, host)

    rig.harness.start(_task(tmp_path, selection_for(host)))

    command = rig.launches[0]
    assert "--model" not in command
    assert "--thinking" not in command
    assert "--effort" not in command
    assert rig.probes == []


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_pinned_controls_are_submitted_as_the_hosts_own_native_flags(
    tmp_path: Path, host: str
) -> None:
    model, effort_flag, *_ = _HOSTS[host]
    rig = _rig(tmp_path, host)

    rig.harness.start(_task(tmp_path, selection_for(host, model=model, effort="high")))

    command = rig.launches[0]
    assert _contains(command, ("--model", model))
    assert _contains(command, (effort_flag, "high"))
    assert len(rig.probes) == 1


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_model_only_pin_submits_no_effort_flag(tmp_path: Path, host: str) -> None:
    model, effort_flag, *_ = _HOSTS[host]
    rig = _rig(tmp_path, host)

    rig.harness.start(_task(tmp_path, selection_for(host, model=model)))

    assert _contains(rig.launches[0], ("--model", model))
    assert effort_flag not in rig.launches[0]


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_fingerprint_mismatch_rejects_a_pinned_control_before_any_launch(
    tmp_path: Path, host: str
) -> None:
    model, _, _, mismatched = _HOSTS[host]
    rig = _rig(tmp_path, host, installed=mismatched)

    with pytest.raises(MissingPrerequisiteError, match="native controls are unsupported"):
        rig.harness.start(_task(tmp_path, selection_for(host, effort="high")))
    with pytest.raises(MissingPrerequisiteError, match="native controls are unsupported"):
        rig.harness.supervised_command(
            _task(tmp_path, selection_for(host, model=model)), result_path=tmp_path / "r.json"
        )

    assert rig.launches == []
    assert len(rig.probes) == 2


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_an_unobservable_host_rejects_a_pinned_control_instead_of_guessing(
    tmp_path: Path, host: str
) -> None:
    rig = _Rig(tmp_path, host, installed=None, output="")

    with pytest.raises(MissingPrerequisiteError, match="could not be probed"):
        rig.harness.start(_task(tmp_path, selection_for(host, effort="low")))

    assert rig.launches == []


@pytest.mark.parametrize(
    ("host", "model"),
    [("omp", "opus"), ("omp", "gpt-5.2"), ("claude-code", "opus"), ("claude-code", "sonnet")],
)
def test_an_alias_or_bare_model_that_reaches_an_adapter_is_rejected_before_launch(
    tmp_path: Path, host: str, model: str
) -> None:
    rig = _rig(tmp_path, host)
    good = selection_for(host, model=_HOSTS[host][0])
    forged = replace(
        good,
        model=RequestedControl(value=model, source=good.model.source, pinned=True),
        selected_model=model,
    )

    with pytest.raises(InvalidInputError, match="not an exact"):
        rig.harness.start(_task(tmp_path, forged))

    assert rig.launches == []
    assert rig.probes == []


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_an_effort_outside_the_hosts_vocabulary_is_rejected_before_launch(
    tmp_path: Path, host: str
) -> None:
    rig = _rig(tmp_path, host)
    good = selection_for(host, effort="high")
    forged = replace(
        good,
        effort=RequestedControl(value="ultra", source=good.effort.source, pinned=True),
        selected_effort="ultra",
    )

    with pytest.raises(InvalidInputError, match="not a supported"):
        rig.harness.start(_task(tmp_path, forged))

    assert rig.launches == []


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_selection_for_another_harness_is_rejected(tmp_path: Path, host: str) -> None:
    other = "claude-code" if host == "omp" else "omp"
    rig = _rig(tmp_path, host)

    with pytest.raises(InvalidInputError, match="this adapter runs"):
        rig.harness.start(_task(tmp_path, selection_for(other)))

    assert rig.launches == []


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_a_selection_bound_to_another_catalog_is_rejected(tmp_path: Path, host: str) -> None:
    from enginery.domain.digests import Digest

    rig = _rig(tmp_path, host)
    pinned = selection_for(host, effort="high")
    stale = replace(pinned, catalog_digest=Digest.of_bytes(b"another catalog"))

    with pytest.raises(InvalidInputError, match="different catalog"):
        rig.harness.start(_task(tmp_path, stale))

    assert rig.launches == []


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_the_receipt_records_submitted_tokens_and_the_observed_effective_model(
    tmp_path: Path, host: str
) -> None:
    model, effort_flag, *_ = _HOSTS[host]
    rig = _rig(tmp_path, host)
    selection = selection_for(host, model=model, effort="high")
    session = rig.harness.start(_task(tmp_path, selection))

    tuple(rig.harness.events(session))
    receipt = rig.harness.result(session).execution_receipt

    assert receipt is not None
    assert receipt.selection_digest == selection.digest
    assert receipt.submitted_arguments == ("--model", model, effort_flag, "high")
    assert receipt.effective_model.state is EffectiveState.OBSERVED
    assert receipt.effective_effort.state is EffectiveState.UNKNOWN
    assert receipt.effective_effort.value is None


@pytest.mark.parametrize("host", ["omp", "claude-code"])
def test_an_unreported_model_stays_unknown_and_never_matches_the_request(
    tmp_path: Path, host: str
) -> None:
    model, *_ = _HOSTS[host]
    rig = _rig(tmp_path, host, with_model=False)
    session = rig.harness.start(_task(tmp_path, selection_for(host, model=model)))

    tuple(rig.harness.events(session))
    receipt = rig.harness.result(session).execution_receipt

    assert receipt is not None
    assert receipt.effective_model.state is EffectiveState.UNKNOWN
    assert receipt.effective_model.value is None


def test_omp_effective_model_is_provider_qualified_from_message_end(tmp_path: Path) -> None:
    rig = _rig(tmp_path, "omp")
    session = rig.harness.start(_task(tmp_path, selection_for("omp", model="openai/gpt-5.2")))

    tuple(rig.harness.events(session))
    receipt = rig.harness.result(session).execution_receipt

    assert receipt is not None
    assert receipt.effective_model.value == "openai/gpt-5.2"
    assert "message_end" in receipt.effective_model.source


def test_claude_effective_model_is_the_assistant_message_model(tmp_path: Path) -> None:
    rig = _rig(tmp_path, "claude-code")
    session = rig.harness.start(_task(tmp_path, selection_for("claude-code")))

    tuple(rig.harness.events(session))
    receipt = rig.harness.result(session).execution_receipt

    assert receipt is not None
    assert receipt.effective_model.value == "claude-opus-4-8"
    assert receipt.submitted_arguments == ()
    assert receipt.effective_model.source == "assistant.message.model"


def test_a_served_model_that_differs_from_the_pin_is_reported_as_observed_not_assumed(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, "claude-code")
    session = rig.harness.start(
        _task(tmp_path, selection_for("claude-code", model="claude-sonnet-5-5"))
    )

    tuple(rig.harness.events(session))
    receipt = rig.harness.result(session).execution_receipt

    assert receipt is not None
    assert receipt.effective_model.value == "claude-opus-4-8"
    assert receipt.effective_model.value != "claude-sonnet-5-5"
