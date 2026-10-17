from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from tests.workflows.test_stage1_behavioral import _ready_to_verify
from tests.workflows.test_stage1_runtime import RecordingPullRequests

from enginery.application.work_ports import PullRequestCheck, PullRequestEvidence, PullRequestReview
from enginery.domain.errors import StaleEvidenceError, TransientProviderFailureError
from enginery.ledger.service import LedgerService
from enginery.workflows.pull_request import PullRequestOutcome
from enginery.workflows.stage1 import Stage1ProgressionAction


@pytest.mark.parametrize("kind", ["review", "status", "check"])
def test_external_veto_is_queued_and_resume_rereads_without_implementation(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    original = RecordingPullRequests.evidence
    calls = 0
    resolved = False

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        nonlocal calls
        calls += 1
        evidence = original(provider, number)
        if resolved:
            return evidence
        if kind == "review":
            return replace(
                evidence,
                reviews=(PullRequestReview("alice", "CHANGES_REQUESTED", "head-revision"),),
            )
        if kind == "status":
            return replace(evidence, combined_status="failure")
        return replace(
            evidence,
            checks=(
                *evidence.checks,
                PullRequestCheck("optional", "completed", "failure", "head-revision"),
            ),
        )

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    result = service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert result.outcome is PullRequestOutcome.BLOCKED
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:verify"
    )
    assert projection is not None
    assert projection.state["status"] == "queued"
    assert projection.state["reason"] == "github_evidence_blocked"
    assert projection.state["repair"]
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.VERIFY
    resolved = True
    result = service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert result.outcome is PullRequestOutcome.MERGE_READY
    assert calls == 3


@pytest.mark.parametrize(
    "error",
    [StaleEvidenceError("status changed"), TransientProviderFailureError("malformed review")],
)
def test_incomplete_observation_clears_previous_current_head_evidence(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    original = RecordingPullRequests.evidence
    broken = False

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        if broken:
            raise error
        return replace(original(provider, number), mergeable=None)

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    broken = True
    result = service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert result.outcome is PullRequestOutcome.WAITING
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:verify"
    )
    assert projection is not None
    state = projection.state
    assert state["reason"] == "github_evidence_incomplete"
    assert state["repair"]
    assert state["status"] == "queued"
    for key in (
        "github_evidence",
        "github_evidence_digest",
        "github_evidence_observations",
        "pull_request_outcome",
        "head_revision",
    ):
        assert state[key] is None
