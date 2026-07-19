from __future__ import annotations

import pytest

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import WorkItemId
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.workflows.issue_to_pr import (
    IssueReadiness,
    Stage1TerminalState,
    issue_to_pr_manifest,
    qualify_issue,
)


def _snapshot(
    *, risk: RiskClass = RiskClass.LOW, work_kind: WorkKind = WorkKind.ISSUE
) -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("work-1"),
            work_kind=work_kind,
            source_provider="github",
            external_reference="https://example.invalid/issues/1",
            source_snapshot_reference="issue:1@42",
            title="Bounded change",
            objective="Change one bounded behavior.",
            acceptance_criteria=("observable result", "focused test"),
            constraints=("retain evidence",),
            risk_class=risk,
            repository_targets=("repository-1",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="42",
    )


def test_qualification_routes_low_risk_applicable_issue_to_implementation() -> None:
    qualification = qualify_issue(_snapshot(), applicable_criteria=(True, False))

    assert qualification.readiness is IssueReadiness.READY
    assert not qualification.requires_human_review
    assert qualification.source_revision == "42"


def test_qualification_requires_human_approval_for_medium_risk() -> None:
    qualification = qualify_issue(
        _snapshot(risk=RiskClass.MEDIUM), applicable_criteria=(True, True)
    )

    assert qualification.readiness is IssueReadiness.AWAITING_PLAN_APPROVAL
    assert qualification.requires_human_review


def test_qualification_rejects_unaligned_applicability() -> None:
    with pytest.raises(InvalidInputError, match="align exactly"):
        qualify_issue(_snapshot(), applicable_criteria=(True,))


def test_manifest_declares_implementation_to_merge_ready_route() -> None:
    manifest = issue_to_pr_manifest()

    assert {"qualify", "implement", "validate", "review", "open_pr", "verify"} <= {
        str(node_id) for node_id in manifest.nodes
    }
    verify = next(node_id for node_id in manifest.nodes if str(node_id) == "verify")
    assert manifest.terminal_state_mapping[verify] == Stage1TerminalState.MERGE_READY.value
