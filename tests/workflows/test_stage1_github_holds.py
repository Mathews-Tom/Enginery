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


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("kind", ["review", "status", "check"])
@pytest.mark.parametrize("node_id", ["wait_for_ci", "verify"])
def test_external_veto_is_queued_and_resume_rereads_without_implementation(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    node_id: str,
    running: bool,
) -> None:
    request, service, now = _ready_to_verify(
        ledger_service, tmp_path, observe_ci=node_id == "verify"
    )
    advance = service.wait_for_ci if node_id == "wait_for_ci" else service.verify_merge_ready
    original = RecordingPullRequests.evidence
    calls = 0
    resolved = False

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        nonlocal calls
        calls += 1
        evidence = original(provider, number)
        if running and not resolved:
            evidence = replace(
                evidence,
                checks=tuple(
                    replace(check, status="in_progress", conclusion=None)
                    for check in evidence.checks
                ),
            )
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
    result = advance(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert (
        result if isinstance(result, PullRequestOutcome) else result.outcome
    ) is PullRequestOutcome.BLOCKED
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:{node_id}"
    )
    assert projection is not None
    assert projection.state["status"] == "queued"
    assert projection.state["reason"] == "github_evidence_blocked"
    assert projection.state["repair"]
    repair = str(projection.state["repair"])
    assert "--database" in repair
    assert "--owner" in repair
    assert "engine next" in repair
    assert "enginery continue" not in repair
    assert "enginery review" in repair
    assert projection.state["head_revision"] == "head-revision"
    assert projection.state["configuration_snapshot_digest"] == str(
        request.run.configuration_snapshot_digest
    )
    expected_action = (
        Stage1ProgressionAction.WAIT_FOR_CI
        if node_id == "wait_for_ci"
        else Stage1ProgressionAction.VERIFY
    )
    assert service.next_action(request.run.id).action is expected_action
    resolved = True
    result = advance(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert (
        result if isinstance(result, PullRequestOutcome) else result.outcome
    ) is PullRequestOutcome.MERGE_READY
    assert calls == (2 if node_id == "wait_for_ci" else 3)
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:{node_id}"
    )
    assert projection is not None
    assert projection.state["head_revision"] == "head-revision"
    assert projection.state["configuration_snapshot_digest"] == str(
        request.run.configuration_snapshot_digest
    )


@pytest.mark.parametrize("node_id", ["wait_for_ci", "verify"])
@pytest.mark.parametrize(
    "error",
    [StaleEvidenceError("status changed"), TransientProviderFailureError("malformed review")],
)
def test_incomplete_observation_clears_previous_current_head_evidence(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    node_id: str,
) -> None:
    request, service, now = _ready_to_verify(
        ledger_service, tmp_path, observe_ci=node_id == "verify"
    )
    advance = service.wait_for_ci if node_id == "wait_for_ci" else service.verify_merge_ready
    original = RecordingPullRequests.evidence
    broken = False

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        if broken:
            raise error
        return replace(original(provider, number), mergeable=None)

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    advance(request, now=now, heartbeat_window=timedelta(seconds=60))
    broken = True
    result = advance(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert (
        result if isinstance(result, PullRequestOutcome) else result.outcome
    ) is PullRequestOutcome.WAITING
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:{node_id}"
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
        "configuration_snapshot_digest",
    ):
        assert state[key] is None


@pytest.mark.parametrize("kind", ["required", "closed", "conflicts"])
def test_verify_block_repairs_describe_the_actual_cause(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    original = RecordingPullRequests.evidence

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        evidence = original(provider, number)
        if kind == "required":
            return replace(
                evidence,
                checks=tuple(
                    replace(check, status="completed", conclusion="failure")
                    for check in evidence.checks
                ),
            )
        if kind == "closed":
            return replace(evidence, pull_request=replace(evidence.pull_request, state="closed"))
        return replace(evidence, mergeable=False)

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    result = service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert result.outcome is PullRequestOutcome.BLOCKED
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:verify"
    )
    assert projection is not None
    assert projection.state["status"] == ("failed" if kind == "required" else "queued")
    assert {
        "required": "this run cannot advance",
        "closed": "reopen a closed PR",
        "conflicts": "resolve its merge conflicts",
    }[kind] in str(projection.state["repair"])
    if kind == "required":
        assert "engine next" not in str(projection.state["repair"])


@pytest.mark.parametrize("node_id", ["wait_for_ci", "verify"])
def test_externally_merged_pr_is_terminal(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node_id: str,
) -> None:
    request, service, now = _ready_to_verify(
        ledger_service, tmp_path, observe_ci=node_id == "verify"
    )
    original = RecordingPullRequests.evidence

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        evidence = original(provider, number)
        return replace(
            evidence,
            pull_request=replace(evidence.pull_request, state="closed", merged=True),
        )

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    advance = service.wait_for_ci if node_id == "wait_for_ci" else service.verify_merge_ready
    advance(request, now=now, heartbeat_window=timedelta(seconds=60))
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:{node_id}"
    )
    assert projection is not None
    assert projection.state["status"] == "failed"
    assert projection.state["reason"] == "github_pull_request_merged"
    assert "merged outside Enginery before verification" in str(projection.state["repair"])
    assert "reopen" not in str(projection.state["repair"])
    assert "engine next" not in str(projection.state["repair"])
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW


def test_required_check_failure_without_repair_budget_has_no_advance_command(
    ledger_service: LedgerService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, service, now = _ready_to_verify(
        ledger_service, tmp_path, observe_ci=False, repair_limit=0
    )
    original = RecordingPullRequests.evidence

    def observe(provider: RecordingPullRequests, number: int) -> PullRequestEvidence:
        evidence = original(provider, number)
        return replace(
            evidence,
            checks=tuple(
                replace(check, status="completed", conclusion="failure")
                for check in evidence.checks
            ),
        )

    monkeypatch.setattr(RecordingPullRequests, "evidence", observe)
    service.wait_for_ci(request, now=now, heartbeat_window=timedelta(seconds=60))
    projection = ledger_service.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{request.run.id}:wait_for_ci"
    )
    assert projection is not None
    assert projection.state["status"] == "failed"
    assert "this run cannot advance" in str(projection.state["repair"])
    assert "--supersede" in str(projection.state["repair"])
    assert "new run id" in str(projection.state["repair"])
    assert "engine next" not in str(projection.state["repair"])
