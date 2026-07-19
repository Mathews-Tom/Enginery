"""Coordinator-owned Stage 1 deterministic node composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from enginery.application.work_ports import WorkLedgerPort, WorkLedgerSnapshot
from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
from enginery.workflows.issue_to_pr import IssueQualification, IssueReadiness, qualify_issue


@dataclass(frozen=True, slots=True)
class Stage1QualificationExecutor:
    """Persist source-bound issue qualification through the shared runtime."""

    runtime: CoordinatorRuntime
    work_ledger: WorkLedgerPort

    def qualify(
        self,
        *,
        request: FixtureDispatch,
        external_reference: str,
        applicable_criteria: tuple[bool, ...],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> IssueQualification:
        """Fetch and qualify an issue after recording its manifest node durably."""
        epoch = self.runtime.register_node(
            request=request, now=now, heartbeat_window=heartbeat_window
        )
        snapshot = self.work_ledger.fetch(external_reference)
        qualification = qualify_issue(snapshot, applicable_criteria=applicable_criteria)
        details = _qualification_details(snapshot, qualification)
        if qualification.readiness is IssueReadiness.READY:
            self.runtime.complete_node(
                run_id=request.run_id,
                node_id=request.node_id,
                epoch=epoch.epoch,
                now=now,
                extra=details,
            )
        else:
            self.runtime.await_human_node(
                run_id=request.run_id,
                node_id=request.node_id,
                epoch=epoch.epoch,
                now=now,
                reason=qualification.reason,
                extra=details,
            )
        return qualification


def _qualification_details(
    snapshot: WorkLedgerSnapshot, qualification: IssueQualification
) -> dict[str, object]:
    return {
        "external_reference": str(snapshot.work_item.external_reference),
        "source_revision": qualification.source_revision,
        "source_digest": qualification.source_digest,
        "readiness": qualification.readiness.value,
    }


__all__ = ["Stage1QualificationExecutor"]
