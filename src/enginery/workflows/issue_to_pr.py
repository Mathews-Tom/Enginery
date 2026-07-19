"""Versioned Stage 1 issue-to-merge-ready workflow contract."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import NodeId, WorkflowDefinitionId
from enginery.domain.workflow.manifest import WorkflowManifest
from enginery.domain.workflow.node import (
    ActorType,
    BranchCondition,
    BranchOperator,
    IdempotencyBehavior,
    NodeDeclaration,
    NodeKind,
    SideEffectClass,
)
from enginery.domain.workflow.schema import FieldSchema, FieldType, IOSchema


class IssueReadiness(enum.StrEnum):
    """Qualification outcomes before any Stage 1 side effect."""

    READY = "ready"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    AWAITING_NO_CHANGE_CONFIRMATION = "awaiting_no_change_confirmation"
    REJECTED = "rejected"


class Stage1TerminalState(enum.StrEnum):
    """Terminal outcomes declared by the Stage 1 workflow manifest."""

    MERGE_READY = "merge_ready"
    NO_CHANGE_REQUIRED = "no_change_required"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class IssueQualification:
    """A source-bound qualification decision for one normalized issue."""

    source_revision: str
    source_digest: str
    readiness: IssueReadiness
    requires_human_review: bool
    reason: str

    def __post_init__(self) -> None:
        if not self.source_revision.strip() or not self.source_digest.strip():
            raise InvalidInputError("qualification requires a non-blank source revision and digest")
        if not self.reason.strip():
            raise InvalidInputError("qualification reason must be non-blank")


def qualify_issue(
    snapshot: WorkLedgerSnapshot,
    *,
    applicable_criteria: tuple[bool, ...],
) -> IssueQualification:
    """Qualify issue readiness without guessing applicability or risk."""

    work_item = snapshot.work_item
    if work_item.work_kind is not WorkKind.ISSUE:
        return _qualification(snapshot, IssueReadiness.REJECTED, "source is not an issue")
    if len(applicable_criteria) != len(work_item.acceptance_criteria):
        raise InvalidInputError(
            "applicable criteria must align exactly with work-item acceptance criteria",
            details={
                "criteria": len(work_item.acceptance_criteria),
                "applicability": len(applicable_criteria),
            },
        )
    if not any(applicable_criteria):
        return _qualification(
            snapshot,
            IssueReadiness.AWAITING_NO_CHANGE_CONFIRMATION,
            "all acceptance criteria require an independent human non-applicability confirmation",
        )
    if work_item.risk_class in {RiskClass.MEDIUM, RiskClass.HIGH}:
        return _qualification(
            snapshot,
            IssueReadiness.AWAITING_PLAN_APPROVAL,
            "medium/high-risk issue requires a current human plan approval",
        )
    return _qualification(
        snapshot, IssueReadiness.READY, "issue has applicable acceptance criteria"
    )


def _qualification(
    snapshot: WorkLedgerSnapshot, readiness: IssueReadiness, reason: str
) -> IssueQualification:
    return IssueQualification(
        source_revision=snapshot.source_revision,
        source_digest=str(snapshot.work_item.bound_field_digest),
        readiness=readiness,
        requires_human_review=readiness
        in {
            IssueReadiness.AWAITING_PLAN_APPROVAL,
            IssueReadiness.AWAITING_NO_CHANGE_CONFIRMATION,
        },
        reason=reason,
    )


def issue_to_pr_manifest() -> WorkflowManifest:
    """Return the immutable Stage 1 graph executed by coordinator-owned nodes."""

    nodes = {
        NodeId("qualify"): _node(
            "qualify",
            NodeKind.NORMALIZE_WORK,
            ActorType.DETERMINISTIC,
            evidence=("source_snapshot", "qualification"),
            output=("readiness",),
        ),
        NodeId("low_risk_route"): _node(
            "low_risk_route",
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=("qualify",),
            conditions=("readiness", IssueReadiness.READY.value),
        ),
        NodeId("plan_approval"): _node(
            "plan_approval",
            NodeKind.REQUEST_HUMAN_DECISION,
            ActorType.HUMAN,
            dependencies=("qualify",),
            conditions=("readiness", IssueReadiness.AWAITING_PLAN_APPROVAL.value),
            evidence=("plan_approval",),
            output=("human_decision",),
        ),
        NodeId("plan_rejected"): _node(
            "plan_rejected",
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=("plan_approval",),
            conditions=("human_decision", "rejected"),
        ),
        NodeId("no_change_confirmation"): _node(
            "no_change_confirmation",
            NodeKind.REQUEST_HUMAN_DECISION,
            ActorType.HUMAN,
            dependencies=("qualify",),
            conditions=("readiness", IssueReadiness.AWAITING_NO_CHANGE_CONFIRMATION.value),
            evidence=("non_applicability_confirmation",),
            output=("human_decision",),
        ),
        NodeId("no_change_required"): _node(
            "no_change_required",
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=("no_change_confirmation",),
            conditions=("human_decision", "approved"),
        ),
        NodeId("no_change_rejected"): _node(
            "no_change_rejected",
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=("no_change_confirmation",),
            conditions=("human_decision", "rejected"),
        ),
        NodeId("rejected"): _node(
            "rejected",
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=("qualify",),
            conditions=("readiness", IssueReadiness.REJECTED.value),
        ),
    }
    low_nodes, low_terminals = _execution_path("low", "low_risk_route")
    approved_nodes, approved_terminals = _execution_path(
        "approved",
        "plan_approval",
        entry_conditions=("human_decision", "approved"),
    )
    nodes.update(low_nodes)
    nodes.update(approved_nodes)
    terminal_mapping = {
        NodeId("plan_rejected"): Stage1TerminalState.REJECTED.value,
        NodeId("no_change_required"): Stage1TerminalState.NO_CHANGE_REQUIRED.value,
        NodeId("no_change_rejected"): Stage1TerminalState.REJECTED.value,
        NodeId("rejected"): Stage1TerminalState.REJECTED.value,
        **low_terminals,
        **approved_terminals,
    }
    return WorkflowManifest(
        id=WorkflowDefinitionId("issue-to-pr-v1"),
        name="issue-to-merge-ready-pull-request",
        schema_version=1,
        nodes=nodes,
        terminal_states=frozenset(state.value for state in Stage1TerminalState),
        terminal_state_mapping=terminal_mapping,
        input_schema=IOSchema(
            fields=(
                FieldSchema("external_reference", FieldType.STRING),
                FieldSchema("repository", FieldType.STRING),
                FieldSchema("base_revision", FieldType.STRING),
            )
        ),
        output_schema=IOSchema(
            fields=(
                FieldSchema("terminal_state", FieldType.STRING),
                FieldSchema("evidence_digest", FieldType.STRING, required=False),
            )
        ),
        compatibility={"workflow": "issue-to-pr-v1"},
    )


def _execution_path(
    prefix: str,
    entry_node: str,
    *,
    entry_conditions: tuple[str, str] | None = None,
) -> tuple[dict[NodeId, NodeDeclaration], dict[NodeId, str]]:
    """Build one mutually exclusive qualified execution route."""

    source_workspace = f"{prefix}_source_before_workspace"
    workspace = f"{prefix}_workspace"
    implement = f"{prefix}_implement"
    source_validation = f"{prefix}_source_before_validation"
    validate = f"{prefix}_validate"
    review = f"{prefix}_review"
    repair = f"{prefix}_repair"
    source_pull_request = f"{prefix}_source_before_pull_request"
    pull_request = f"{prefix}_pull_request"
    wait_for_ci = f"{prefix}_wait_for_ci"
    source_terminal = f"{prefix}_source_before_terminal"
    verify = f"{prefix}_verify"
    nodes = {
        NodeId(source_workspace): _source_check(
            source_workspace, entry_node, conditions=entry_conditions
        ),
        NodeId(workspace): _node(
            workspace,
            NodeKind.CREATE_OR_CLEAN_WORKSPACE,
            ActorType.DETERMINISTIC,
            dependencies=(source_workspace,),
            conditions=("source_current", "current"),
            side_effecting=True,
            reconciliation="workspace reservation",
            evidence=("workspace_reservation", "base_revision"),
        ),
        NodeId(implement): _node(
            implement,
            NodeKind.EXECUTE_AGENT_TASK,
            ActorType.AGENT,
            dependencies=(workspace,),
            side_effecting=True,
            reconciliation="harness operation",
            evidence=("harness_result", "redacted_outputs"),
        ),
        NodeId(source_validation): _source_check(source_validation, implement),
        NodeId(validate): _node(
            validate,
            NodeKind.RUN_COMMAND,
            ActorType.DETERMINISTIC,
            dependencies=(source_validation,),
            conditions=("source_current", "current"),
            side_effecting=True,
            reconciliation="validation operation",
            evidence=("validation_result", "diff"),
        ),
        NodeId(review): _node(
            review,
            NodeKind.REQUEST_HUMAN_DECISION,
            ActorType.HUMAN,
            dependencies=(validate,),
            evidence=("review",),
            output=("review_decision",),
        ),
        NodeId(repair): _node(
            repair,
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=(review,),
            conditions=("review_decision", "repair_requested"),
            evidence=("repair_route",),
        ),
        NodeId(f"{prefix}_review_rejected"): _node(
            f"{prefix}_review_rejected",
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=(review,),
            conditions=("review_decision", "rejected"),
        ),
        NodeId(source_pull_request): _source_check(
            source_pull_request,
            review,
            conditions=("review_decision", "approved"),
        ),
        NodeId(pull_request): _node(
            pull_request,
            NodeKind.OPEN_OR_UPDATE_PULL_REQUEST,
            ActorType.DETERMINISTIC,
            dependencies=(source_pull_request,),
            conditions=("source_current", "current"),
            side_effecting=True,
            reconciliation="pull request operation marker",
            evidence=("pull_request",),
        ),
        NodeId(wait_for_ci): _node(
            wait_for_ci,
            NodeKind.WAIT_FOR_CI,
            ActorType.DETERMINISTIC,
            dependencies=(pull_request,),
            evidence=("exact_head_ci",),
        ),
        NodeId(source_terminal): _source_check(source_terminal, wait_for_ci),
        NodeId(verify): _node(
            verify,
            NodeKind.VERIFY_EVIDENCE,
            ActorType.DETERMINISTIC,
            dependencies=(source_terminal,),
            conditions=("source_current", "current"),
            evidence=("terminal_double_read", "merge_ready_bundle"),
            output=("verification_outcome",),
        ),
    }
    outcomes = ("merge_ready", "blocked", "cancelled", "superseded")
    for outcome in outcomes:
        terminal_node = f"{prefix}_{outcome}"
        nodes[NodeId(terminal_node)] = _node(
            terminal_node,
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=(verify,),
            conditions=("verification_outcome", outcome),
        )
    for source_node in (source_workspace, source_validation, source_pull_request, source_terminal):
        terminal_node = f"{source_node}_superseded"
        nodes[NodeId(terminal_node)] = _node(
            terminal_node,
            NodeKind.ROUTE,
            ActorType.DETERMINISTIC,
            dependencies=(source_node,),
            conditions=("source_current", "stale"),
        )
    terminal_mapping = {
        NodeId(f"{prefix}_merge_ready"): Stage1TerminalState.MERGE_READY.value,
        NodeId(f"{prefix}_blocked"): Stage1TerminalState.BLOCKED.value,
        NodeId(f"{prefix}_cancelled"): Stage1TerminalState.CANCELLED.value,
        NodeId(f"{prefix}_superseded"): Stage1TerminalState.SUPERSEDED.value,
        NodeId(f"{prefix}_review_rejected"): Stage1TerminalState.REJECTED.value,
        NodeId(f"{prefix}_repair"): Stage1TerminalState.BLOCKED.value,
        **{
            NodeId(f"{source_node}_superseded"): Stage1TerminalState.SUPERSEDED.value
            for source_node in (
                source_workspace,
                source_validation,
                source_pull_request,
                source_terminal,
            )
        },
    }
    return nodes, terminal_mapping


def _source_check(
    node_id: str,
    dependency: str,
    *,
    conditions: tuple[str, str] | None = None,
) -> NodeDeclaration:
    return _node(
        node_id,
        NodeKind.VERIFY_EVIDENCE,
        ActorType.DETERMINISTIC,
        dependencies=(dependency,),
        conditions=conditions,
        evidence=("source_revision", "bound_field_digest"),
        output=("source_current",),
    )


def _node(
    node_id: str,
    kind: NodeKind,
    actor_type: ActorType,
    *,
    dependencies: tuple[str, ...] = (),
    conditions: tuple[str, str] | None = None,
    side_effecting: bool = False,
    reconciliation: str | None = None,
    evidence: tuple[str, ...] = (),
    output: tuple[str, ...] = (),
) -> NodeDeclaration:
    return NodeDeclaration(
        node_id=NodeId(node_id),
        kind=kind,
        input_schema=IOSchema(),
        output_schema=IOSchema(tuple(FieldSchema(name, FieldType.STRING) for name in output)),
        actor_type=actor_type,
        side_effect_class=(
            SideEffectClass.SIDE_EFFECTING if side_effecting else SideEffectClass.NONE
        ),
        idempotency_behavior=(
            IdempotencyBehavior.RECONCILIATION_QUERY
            if side_effecting
            else IdempotencyBehavior.NOT_APPLICABLE
        ),
        reconciliation_operation=reconciliation,
        evidence_contract=evidence,
        dependencies=tuple(NodeId(dependency) for dependency in dependencies),
        branch_conditions=(
            (
                BranchCondition(
                    BranchOperator.EQUALS,
                    field_path=conditions[0],
                    values=(conditions[1],),
                ),
            )
            if conditions is not None
            else ()
        ),
        emitted_event_types=(f"stage1.{node_id}",),
    )


__all__ = [
    "IssueQualification",
    "IssueReadiness",
    "Stage1TerminalState",
    "issue_to_pr_manifest",
    "qualify_issue",
]
