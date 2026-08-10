"""Stage 1 issue qualification and its versioned execution graph."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.workflow.manifest import WorkflowManifest


class WorkReadiness(enum.StrEnum):
    """Qualification outcomes before Stage 1 performs a mutation."""

    READY = "ready"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    AWAITING_NO_CHANGE_CONFIRMATION = "awaiting_no_change_confirmation"
    REJECTED = "rejected"


class Stage1TerminalState(enum.StrEnum):
    """Closed outcomes of a qualified-work-to-merge-ready run."""

    MERGE_READY = "merge_ready"
    NO_CHANGE_REQUIRED = "no_change_required"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class WorkQualification:
    """A source-bound intent decision for one normalized work item."""

    source_revision: str
    source_digest: str
    readiness: WorkReadiness
    requires_human_review: bool
    reason: str

    def __post_init__(self) -> None:
        if not self.source_revision.strip() or not self.source_digest.strip():
            raise InvalidInputError("qualification requires source revision and digest")
        if not self.reason.strip():
            raise InvalidInputError("qualification requires a reason")


def qualify_work(
    snapshot: WorkLedgerSnapshot, *, applicable_criteria: tuple[bool, ...]
) -> WorkQualification:
    """Classify supported work without inferring applicability or risk approval."""
    work_item = snapshot.work_item
    if work_item.work_kind not in {WorkKind.ISSUE, WorkKind.PLAN}:
        return _qualification(
            snapshot,
            WorkReadiness.REJECTED,
            f"Stage 1 does not support work kind {work_item.work_kind.value}",
        )
    if len(applicable_criteria) != len(work_item.acceptance_criteria):
        raise InvalidInputError("applicability must align exactly with acceptance criteria")
    if not any(applicable_criteria):
        return _qualification(
            snapshot,
            WorkReadiness.AWAITING_NO_CHANGE_CONFIRMATION,
            "all acceptance criteria require human non-applicability confirmation",
        )
    if work_item.risk_class is RiskClass.HIGH:
        return _qualification(
            snapshot,
            WorkReadiness.REJECTED,
            "Stage 1 does not support high-risk work",
        )
    if work_item.risk_class is RiskClass.MEDIUM:
        return _qualification(
            snapshot,
            WorkReadiness.AWAITING_PLAN_APPROVAL,
            "medium-risk work requires human plan approval",
        )
    return _qualification(snapshot, WorkReadiness.READY, "work has applicable acceptance criteria")


def stage1_work_manifest() -> WorkflowManifest:
    """Return the static Stage 1 graph executed under coordinator ownership."""
    nodes = {
        "qualify": _node("normalize_work", "deterministic"),
        "plan_approval": _node("request_human_decision", "human", dependencies=("qualify",)),
        "no_change_confirmation": _node(
            "request_human_decision", "human", dependencies=("qualify",)
        ),
        "implement": _node(
            "execute_agent_task", "agent", dependencies=("qualify",), side_effecting=True
        ),
        "validate": _node("run_command", "deterministic", dependencies=("implement",)),
        "review": _node("request_human_decision", "human", dependencies=("validate",)),
        "repair": _node("route", "deterministic", dependencies=("review",)),
        "open_pr": _node(
            "open_or_update_pull_request",
            "deterministic",
            dependencies=("review",),
            side_effecting=True,
        ),
        "wait_for_ci": _node("wait_for_ci", "deterministic", dependencies=("open_pr",)),
        "verify": _node("verify_evidence", "deterministic", dependencies=("wait_for_ci",)),
    }
    return WorkflowManifest.from_mapping(
        {
            "id": "issue-to-pr-v1",
            "name": "qualified-work-to-merge-ready-pull-request",
            "schema_version": 1,
            "nodes": nodes,
            "terminal_states": [state.value for state in Stage1TerminalState],
            "terminal_state_mapping": {"verify": Stage1TerminalState.MERGE_READY.value},
        }
    )


def _qualification(
    snapshot: WorkLedgerSnapshot, readiness: WorkReadiness, reason: str
) -> WorkQualification:
    return WorkQualification(
        source_revision=snapshot.source_revision,
        source_digest=str(snapshot.bound_digest),
        readiness=readiness,
        requires_human_review=readiness
        in {
            WorkReadiness.AWAITING_PLAN_APPROVAL,
            WorkReadiness.AWAITING_NO_CHANGE_CONFIRMATION,
        },
        reason=reason,
    )


def _node(
    kind: str,
    actor_type: str,
    *,
    dependencies: tuple[str, ...] = (),
    side_effecting: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "input_schema": {},
        "output_schema": {},
        "actor_type": actor_type,
        "side_effect_class": "side_effecting" if side_effecting else "none",
        "idempotency_behavior": "reconciliation_query" if side_effecting else "not_applicable",
        "reconciliation_operation": kind if side_effecting else None,
        "dependencies": list(dependencies),
    }


__all__ = [
    "Stage1TerminalState",
    "WorkQualification",
    "WorkReadiness",
    "qualify_work",
    "stage1_work_manifest",
]
