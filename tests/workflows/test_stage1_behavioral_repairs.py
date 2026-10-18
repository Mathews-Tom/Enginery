"""Real Stage 1 verification routes semantic failures and retries infrastructure."""

from __future__ import annotations

import socket
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.evidence.test_behavioral_acceptance import _repository
from tests.workflows.test_stage1_behavioral import _ready_to_verify
from tests.workflows.test_stage1_runtime import RecordingPullRequests

from enginery.application.behavioral_checks import (
    BehavioralAssertion,
    BehavioralCheck,
    BehavioralChecksBinding,
)
from enginery.application.work_ports import PullRequestEvidence
from enginery.cli import factory_behavioral, factory_collect
from enginery.domain.digests import Digest
from enginery.domain.errors import HumanActionRequiredError
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from enginery.workflows import stage1
from enginery.workflows.behavioral_acceptance import BehavioralAcceptanceService
from enginery.workflows.stage1 import Stage1ProgressionAction, Stage1RunRequest


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    request: Stage1RunRequest,
    checkout: Path,
    check: BehavioralCheck,
    custody: Path,
) -> BehavioralAcceptanceService:
    custody.mkdir(mode=0o700)
    observer = BehavioralAcceptanceService(ArtifactStore(custody / "artifacts"), custody)
    binding = BehavioralChecksBinding(
        request.run.base_revision, str(request.run.configuration_snapshot_digest), (check,)
    )
    monkeypatch.setattr(factory_behavioral, "read_behavioral_checks", lambda request: binding)
    monkeypatch.setattr(factory_behavioral, "_service", lambda request: observer)
    monkeypatch.setattr(factory_behavioral, "private_state_directory", lambda repository: custody)
    monkeypatch.setattr(factory_collect, "state_home", lambda: custody)
    return observer


@pytest.mark.parametrize("repair_limit", [0, 1])
def test_first_candidate_failure_is_named_and_takes_bounded_repair(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_limit: int,
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path, repair_limit=repair_limit)
    source = tmp_path / "source"
    source.mkdir()
    checkout, head = _repository(source, "print('regressed', end='')\n")
    check = BehavioralCheck(
        "changed-cli",
        "command",
        (BehavioralAssertion("stdout", "equals", "changed"),),
        ("constant_result", "wrong_build"),
        argv=(sys.executable, "behavior.py"),
    )
    observer = _wire(monkeypatch, request, checkout, check, tmp_path / "evaluator")
    calibrations: list[str] = []
    original = observer.calibrate

    def calibrate(**kwargs: object) -> object:
        calibrations.append("calibrated")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(observer, "calibrate", calibrate)
    bound = replace(request, workspace_path=checkout, repair_limit=repair_limit)
    # Publication fixture uses a symbolic head; observation uses its real committed candidate.
    service = replace(
        service,
        behavioral_verifier=lambda req, revision: factory_behavioral.verify_for_stage1(
            ledger_service, bound, head, owner="operator"
        ),
    )
    with pytest.raises(
        HumanActionRequiredError, match="behavioral_assertion_failed:changed-cli:0"
    ) as caught:
        service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert "calibration" not in str(caught.value)
    assert calibrations == [], "a candidate failure must be observed before calibration"
    state = service._node_state(bound.run.id, "verify")
    assert state["status"] == "failed"
    assert state["behavioral_findings"] == ["behavioral_assertion_failed:changed-cli:0"]
    expected = (
        Stage1ProgressionAction.IMPLEMENT
        if repair_limit
        else Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
    )
    assert service.next_action(bound.run.id).action is expected
    if repair_limit:
        assert "engine next" in str(caught.value.details["repair"])
        assert "commit the corrected head" not in str(caught.value.details["repair"])
        # Exercise Stage 1's real dispatch/repair-context construction, without
        # launching an unrelated harness process from this semantic-verifier test.
        monkeypatch.setattr(
            service.runtime, "tick", lambda **kwargs: SimpleNamespace(dispatched=())
        )
        # The terminal-consumer fixture models implement as deterministic. The
        # scheduler envelope is outside this test; retain its real task/request.
        monkeypatch.setattr(
            stage1,
            "WorkflowDispatch",
            lambda dispatch, manifest: SimpleNamespace(request=dispatch, manifest=manifest),
        )
        repairing = replace(
            service,
            host_probe=lambda: None,
            harness=SimpleNamespace(supervised_command=lambda *args, **kwargs: ("true",)),
        )
        dispatch = repairing.dispatch_implementation(
            request,
            now=now,
            heartbeat_window=timedelta(seconds=60),
            lease_window=timedelta(seconds=60),
            limits=SchedulingLimits(global_concurrency=1, per_repository_concurrency=1),
        )
        assert "behavioral_assertion_failed:changed-cli:0" in " ".join(dispatch.task.constraints)
        assert str(dispatch.task.attempt_id) != request.implementation.attempt_id
    else:
        assert "budget is exhausted" in str(caught.value.details["repair"])
        repair = str(caught.value.details["repair"])
        assert f"repair: {repair}" in str(caught.value)
        assert "this run cannot pass" in repair
        assert "decision" not in repair
        assert "change the issue snapshot" in repair
        assert "enginery run ISSUE --project PROJECT --supersede" in repair
        assert "enginery engine build-request" in repair
        assert "enginery engine build-request --run-id NEW_RUN_ID" in repair
        assert "enginery engine start --request REQUEST" in repair


def test_inconclusive_calibration_retries_after_observer_recovers(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    script = (
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'changed')\n"
        " def log_message(self, *args): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()\n"
    )
    checkout, head = _repository(source, script)
    check = BehavioralCheck(
        "changed-service",
        "service",
        (BehavioralAssertion("body", "equals", "changed"),),
        ("constant_result", "wrong_build"),
        argv=(sys.executable, "behavior.py"),
        url=f"http://127.0.0.1:{port}/",
    )
    observer = _wire(monkeypatch, request, checkout, check, tmp_path / "evaluator")
    bound = replace(request, workspace_path=checkout)
    service = replace(
        service,
        behavioral_verifier=lambda req, revision: factory_behavioral.verify_for_stage1(
            ledger_service, bound, head, owner="operator"
        ),
    )
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        with pytest.raises(HumanActionRequiredError, match="observer_service_conflict") as caught:
            service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
        repair = str(caught.value.details["repair"])
        assert "enginery calibrate run-check" in repair
        assert "--database" in repair and "--owner operator" in repair
        assert list(observer.evaluator_directory.glob("calibration-*.json"))
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.VERIFY
    result = service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert result.outcome.value == "merge_ready"
    projection = ledger_service.read_projection(
        aggregate_type="behavioral_acceptance", aggregate_id=str(request.run.id)
    )
    assert projection is not None
    assert projection.state["result"] == "pass"


def test_missing_composed_observer_does_not_suggest_unconsumed_factory_check(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    with pytest.raises(HumanActionRequiredError) as caught:
        replace(service, behavioral_verifier=None).verify_merge_ready(
            request, now=now, heartbeat_window=timedelta(seconds=60)
        )
    assert "enginery check" not in str(caught.value)
    assert "engine next" in str(caught.value)


def test_behavioral_failure_clears_retryable_github_hold(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path, repair_limit=1)
    original = RecordingPullRequests.evidence
    closed = True
    passing = True

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        evidence = original(provider, number)
        return (
            replace(evidence, pull_request=replace(evidence.pull_request, state="closed"))
            if closed
            else evidence
        )

    def verifier(bound: Stage1RunRequest, head: str) -> Digest:
        if passing:
            return Digest.of_bytes(b"passing-receipt")
        raise HumanActionRequiredError(
            "behavioral_acceptance_failed: behavioral_assertion_failed:check:0",
            details={
                "reason": "behavioral_acceptance_failed",
                "findings": ("behavioral_assertion_failed:check:0",),
            },
        )

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    service = replace(service, behavioral_verifier=verifier)
    heartbeat = timedelta(seconds=60)
    service.verify_merge_ready(request, now=now, heartbeat_window=heartbeat)
    held = service._node_state(request.run.id, "verify")
    assert held["status"] == "queued"
    assert held["reason"] == "github_evidence_blocked"
    closed = False
    passing = False
    with pytest.raises(HumanActionRequiredError, match="behavioral_acceptance_failed"):
        service.verify_merge_ready(request, now=now, heartbeat_window=heartbeat)
    state = service._node_state(request.run.id, "verify")
    assert state["status"] == "failed"
    assert state["behavioral_findings"] == ["behavioral_assertion_failed:check:0"]
    assert all(state.get(field) is None for field in ("reason", "repair", "message"))
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.IMPLEMENT
