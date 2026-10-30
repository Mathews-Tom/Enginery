"""The real Stage 1 terminal consumer must honor the trusted behavioral verdict."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from tests.support.authenticated_decisions import AuthenticatedDecisions
from tests.workflows.test_run_lifecycle import (
    TerminalWorkLedger,
    _bind_publication_base,
    _check_disposition_fixture,
    _complete_check_node,
    _publication_workspace,
)

from enginery.domain.digests import Digest
from enginery.domain.errors import HumanActionRequiredError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.review import ReviewReport
from enginery.workflows.run_intent import Stage1RunRequest
from enginery.workflows.run_lifecycle import Stage1ProgressionAction, Stage1RunService


def _ready_to_verify(
    ledger_service: LedgerService,
    tmp_path: Path,
    *,
    repair_limit: int = 1,
    observe_ci: bool = True,
) -> tuple[Stage1RunRequest, Stage1RunService, datetime]:
    request, service, runtime, pull_requests, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False
    )
    head_revision = _publication_workspace(request)
    request = replace(_bind_publication_base(request), repair_limit=repair_limit)
    pull_requests.base_revision = request.run.base_revision
    service.start(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert request.behavioral_checks
    assert all(check.assertions for check in request.behavioral_checks)
    for node_id in ("qualify", "implement", "validate"):
        _complete_check_node(runtime, request, node_id, now)
    ledger_service.append(
        AppendCommand(
            correlation_id="behavioral-consumer-implementation",
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=request.implementation.attempt_id,
                    expected_version=0,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={
                        "artifact_references": [str(Digest.of_bytes(b"implementation"))],
                        "result": {"head_revision": head_revision},
                    },
                ),
            ),
        )
    )
    heartbeat = timedelta(seconds=60)
    service.review_implementation(
        request,
        ReviewReport(producer="omp-agent", reviewer="operator", findings=()),
        repair_attempt=0,
        now=now,
        heartbeat_window=heartbeat,
    )
    AuthenticatedDecisions(ledger_service).approve(request)
    service.open_pull_request(request, now=now, heartbeat_window=heartbeat)
    if observe_ci:
        service.wait_for_ci(request, now=now, heartbeat_window=heartbeat)
        assert service.next_action(request.run.id).action is Stage1ProgressionAction.VERIFY
    return request, service, now


def test_behavioral_verifier_refusal_prevents_stage1_verify_completion(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    calls: list[tuple[Stage1RunRequest, str]] = []

    def refuse(bound_request: Stage1RunRequest, head_revision: str) -> Digest:
        calls.append((bound_request, head_revision))
        raise HumanActionRequiredError("current behavioral verdict is not passing")

    service = replace(service, behavioral_verifier=refuse)
    work_ledger = cast(TerminalWorkLedger, service.work_ledger)
    before = tuple(work_ledger.published)
    with pytest.raises(HumanActionRequiredError, match="behavioral verdict is not passing"):
        service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert calls == [(request, "head-revision")]
    assert tuple(work_ledger.published) == before
    assert service.next_action(request.run.id).action is Stage1ProgressionAction.VERIFY


def test_behavioral_verifier_digest_enters_stage1_verify_evidence(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, now = _ready_to_verify(ledger_service, tmp_path)
    verdict_digest = Digest.of_bytes(b"trusted-current-passing-behavioral-verdict")
    calls: list[tuple[Stage1RunRequest, str]] = []

    def accept(bound_request: Stage1RunRequest, head_revision: str) -> Digest:
        calls.append((bound_request, head_revision))
        return verdict_digest

    service = replace(service, behavioral_verifier=accept)
    result = service.verify_merge_ready(request, now=now, heartbeat_window=timedelta(seconds=60))
    assert calls == [(request, "head-revision")]
    assert result.outcome.value == "merge_ready"
    assert result.evidence is not None
    assert result.evidence.verification_artifacts == (
        Digest.of_bytes(b"validation"),
        verdict_digest,
    )
    work_ledger = cast(TerminalWorkLedger, service.work_ledger)
    assert work_ledger.published[-1].state == "merge_ready"
    assert work_ledger.published[-1].evidence_digest == result.evidence.digest
    assert service.next_action(request.run.id).action is not Stage1ProgressionAction.VERIFY
