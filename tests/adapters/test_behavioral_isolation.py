"""Isolated candidate commands never use the trusted-local process seam."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest

from enginery.adapters import behavioral_observers
from enginery.adapters.behavioral_observers import observe
from enginery.application.behavioral_checks import BehavioralCheck
from enginery.application.runner_ports import AttestedProcess
from enginery.domain.digests import Digest
from enginery.domain.execution_receipt import RunnerReceiptRef


def _check(observer: str = "command") -> BehavioralCheck:
    return BehavioralCheck.from_state(
        {
            "name": "isolated",
            "observer": observer,
            "argv": ["python3", "candidate.py"],
            "assertions": [
                {
                    "kind": "stdout"
                    if observer == "command"
                    else "body"
                    if observer == "service"
                    else "dom",
                    "contains": "changed",
                    **({"selector": "body"} if observer == "browser" else {}),
                }
            ],
            "fault_classes": ["constant_result"],
            **({} if observer == "command" else {"url": "http://127.0.0.1:1234/"}),
        }
    )


def test_isolated_command_uses_attested_runner_not_host_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], Path]] = []

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("candidate subprocess started on host")

    monkeypatch.setattr(behavioral_observers, "_Process", forbidden)
    reference = RunnerReceiptRef(
        "isolated-oci",
        Digest.of_bytes(b"receipt"),
        "sha256:" + "a" * 64,
        Digest.of_bytes(b"profile"),
    )

    def runner(argv: tuple[str, ...], cwd: Path) -> AttestedProcess:
        calls.append((argv, cwd))
        return AttestedProcess(
            argv,
            0,
            json.dumps({"returncode": 0, "stdout": "changed", "stderr": ""}),
            "",
            runner_receipt=reference,
        )

    data = observe(_check(), cwd=tmp_path, isolated=True, command_runner=runner)
    assert data.ran and data.infrastructure_error is None
    assert data.stdout == "changed"
    assert data.runner_receipt_digest == reference.receipt_digest
    assert len(calls) == 1
    assert calls[0][0][:3] == ("python3", "-I", "-c")
    assert json.loads(calls[0][0][-1]) == ["python3", "candidate.py"]
    assert calls[0][1] == tmp_path


@pytest.mark.parametrize("observer", ["service", "browser"])
def test_isolated_network_observers_hold_without_host_execution(
    tmp_path: Path, observer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("host code executed")

    monkeypatch.setattr(behavioral_observers, "_Process", forbidden)
    data = observe(_check(observer), cwd=tmp_path, isolated=True, command_runner=forbidden)
    assert not data.ran
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("behavioral_observer_requires_isolation:")
    assert "trusted-local" in data.infrastructure_error


def test_isolated_command_without_runner_never_falls_back_to_host(tmp_path: Path) -> None:
    data = observe(_check(), cwd=tmp_path, isolated=True)
    assert not data.ran
    assert data.infrastructure_error is not None
    assert data.infrastructure_error.startswith("behavioral_observer_requires_isolation:")


def test_isolated_request_composition_never_launches_candidate_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from tests.cli.test_stage1 import _request
    from tests.workflows.test_stage1_isolated import _isolated

    from enginery.adapters.oci.admission import OciRunnerAdmission
    from enginery.adapters.oci.validation import OciValidationRunner
    from enginery.application.runner_ports import RunnerAdmissionDecision
    from enginery.cli.config_binding import bind_behavioral_checks
    from enginery.cli.config_locations import private_state_directory
    from enginery.cli.factory_behavioral import _service

    request = _request(tmp_path)
    repository = request.repository_path
    request = replace(
        request,
        runner=_isolated(),
        execution_configuration=replace(
            request.execution_configuration,
            artifact_root=private_state_directory(repository) / "artifacts",
        ),
    )
    bind_behavioral_checks(request)
    calls: list[tuple[str, ...]] = []
    reference = RunnerReceiptRef(
        "isolated-oci",
        Digest.of_bytes(b"runner-receipt"),
        "sha256:" + "a" * 64,
        Digest.of_bytes(b"profile"),
    )

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("isolated candidate executed on the host")

    def contained(self: OciValidationRunner, argv: tuple[str, ...], cwd: Path) -> AttestedProcess:
        calls.append(argv)
        assert self.runner.selection == request.runner
        return AttestedProcess(
            argv,
            0,
            json.dumps({"returncode": 0, "stdout": "fixture", "stderr": ""}),
            "",
            runner_receipt=reference,
        )

    monkeypatch.setattr(behavioral_observers, "_Process", forbidden)
    monkeypatch.setattr(
        OciRunnerAdmission, "admit", lambda self, selection: RunnerAdmissionDecision(None)
    )
    monkeypatch.setattr(OciValidationRunner, "__call__", contained)
    service = _service(request)
    assert request.behavioral_checks
    data = service._observe(request.behavioral_checks[0], repository)
    assert data.ran and data.infrastructure_error is None
    assert data.runner_receipt_digest == reference.receipt_digest
    assert len(calls) == 1
