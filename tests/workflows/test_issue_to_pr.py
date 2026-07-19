from __future__ import annotations

import pytest

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import NodeId, WorkItemId
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.workflows.issue_to_pr import (
    IssueReadiness,
    Stage1TerminalState,
    issue_to_pr_manifest,
    qualify_issue,
)


def _snapshot(
    *, risk_class: RiskClass = RiskClass.LOW, work_kind: WorkKind = WorkKind.ISSUE
) -> WorkLedgerSnapshot:
    return WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("github:owner/repository#1"),
            work_kind=work_kind,
            source_provider="github-issues",
            external_reference="owner/repository#1",
            source_snapshot_reference="https://example.test/issues/1",
            title="Implement Stage 1",
            objective="Implement the requested behavior.",
            acceptance_criteria=("behavior works", "tests pass"),
            constraints=("keep the change focused",),
            risk_class=risk_class,
            repository_targets=("owner/repository",),
            dependencies=(),
            state=WorkItemState.NEW,
        ),
        source_revision="2026-07-19T00:00:00Z:subject-digest",
    )


def test_qualify_low_risk_issue_with_applicable_criteria() -> None:
    qualification = qualify_issue(_snapshot(), applicable_criteria=(True, False))

    assert qualification.readiness is IssueReadiness.READY
    assert qualification.requires_human_review is False
    assert qualification.source_revision == _snapshot().source_revision
    assert qualification.source_digest == str(_snapshot().work_item.bound_field_digest)


@pytest.mark.parametrize("risk_class", (RiskClass.MEDIUM, RiskClass.HIGH))
def test_qualify_medium_or_high_risk_issue_requires_plan_approval(
    risk_class: RiskClass,
) -> None:
    qualification = qualify_issue(
        _snapshot(risk_class=risk_class), applicable_criteria=(True, True)
    )

    assert qualification.readiness is IssueReadiness.AWAITING_PLAN_APPROVAL
    assert qualification.requires_human_review is True


@pytest.mark.parametrize("risk_class", (RiskClass.LOW, RiskClass.HIGH))
def test_qualify_all_non_applicable_issue_requires_human_confirmation(
    risk_class: RiskClass,
) -> None:
    qualification = qualify_issue(
        _snapshot(risk_class=risk_class), applicable_criteria=(False, False)
    )

    assert qualification.readiness is IssueReadiness.AWAITING_NO_CHANGE_CONFIRMATION
    assert qualification.requires_human_review is True


def test_qualify_non_issue_rejects_without_side_effect() -> None:
    qualification = qualify_issue(
        _snapshot(work_kind=WorkKind.PLAN), applicable_criteria=(True, True)
    )

    assert qualification.readiness is IssueReadiness.REJECTED


def test_qualify_rejects_misaligned_applicability() -> None:
    with pytest.raises(InvalidInputError, match="align exactly"):
        qualify_issue(_snapshot(), applicable_criteria=(True,))


def test_manifest_declares_versioned_merge_ready_contract() -> None:
    manifest = issue_to_pr_manifest()

    assert manifest.id.value == "issue-to-pr-v1"
    assert (
        manifest.terminal_state_mapping[NodeId("low_merge_ready")]
        == Stage1TerminalState.MERGE_READY.value
    )
    assert Stage1TerminalState.MERGE_READY.value in manifest.terminal_states
    assert (
        manifest.nodes[NodeId("low_pull_request")].reconciliation_operation
        == "pull request operation marker"
    )


def test_manifest_binds_human_and_source_gates_before_side_effects() -> None:
    manifest = issue_to_pr_manifest()

    approved_entry = manifest.nodes[NodeId("approved_source_before_workspace")]
    assert approved_entry.branch_conditions[0].field_path == "human_decision"
    assert approved_entry.branch_conditions[0].values == ("approved",)
    source_gate = manifest.nodes[NodeId("low_source_before_pull_request")]
    assert source_gate.evidence_contract == ("source_revision", "bound_field_digest")
    assert source_gate.branch_conditions[0].values == ("approved",)


def test_manifest_routes_terminal_states_by_verification_outcome() -> None:
    manifest = issue_to_pr_manifest()

    merge_ready = manifest.nodes[NodeId("low_merge_ready")]
    blocked = manifest.nodes[NodeId("low_blocked")]
    assert merge_ready.branch_conditions[0].field_path == "verification_outcome"
    assert merge_ready.branch_conditions[0].values == ("merge_ready",)
    assert blocked.branch_conditions[0].values == ("blocked",)


def test_manifest_has_no_unmapped_leaf_nodes() -> None:
    manifest = issue_to_pr_manifest()
    dependents = {node_id: set() for node_id in manifest.nodes}
    for node in manifest.nodes.values():
        for dependency in node.dependencies:
            dependents[dependency].add(node.node_id)

    leaves = {node_id for node_id, node_dependents in dependents.items() if not node_dependents}
    assert leaves == set(manifest.terminal_state_mapping)
