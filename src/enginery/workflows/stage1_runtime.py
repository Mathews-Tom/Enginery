"""Coordinator-owned Stage 1 deterministic node composition."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.engine.runtime import CoordinatorRuntime, WorkflowNodeDispatch
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.redaction import redact_credential_shaped_text
from enginery.workflows.issue_to_pr import WorkQualification, WorkReadiness, qualify_work
from enginery.workflows.review import ReviewOutcome, ReviewReport, route_review


@dataclass(frozen=True, slots=True)
class Stage1QualificationExecutor:
    """Persist a pre-bound source qualification through the shared runtime."""

    runtime: CoordinatorRuntime

    def qualify(
        self,
        *,
        dispatch: WorkflowNodeDispatch,
        snapshot: WorkLedgerSnapshot,
        applicable_criteria: tuple[bool, ...],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> WorkQualification:
        """Record qualification only after the caller verifies the source binding."""
        epoch = self.runtime.register_node(
            dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
        )
        qualification = qualify_work(snapshot, applicable_criteria=applicable_criteria)
        details = _qualification_details(snapshot, qualification)
        if qualification.readiness is WorkReadiness.READY:
            self.runtime.complete_node(
                run_id=dispatch.request.run_id,
                node_id=dispatch.request.node_id,
                epoch=epoch.epoch,
                now=now,
                extra=details,
            )
        elif qualification.readiness is WorkReadiness.REJECTED:
            self.runtime.complete_node(
                run_id=dispatch.request.run_id,
                node_id=dispatch.request.node_id,
                epoch=epoch.epoch,
                now=now,
                outcome="blocked",
                extra=details,
            )
        else:
            self.runtime.await_human_node(
                run_id=dispatch.request.run_id,
                node_id=dispatch.request.node_id,
                epoch=epoch.epoch,
                now=now,
                reason=qualification.reason,
                extra=details,
            )
        return qualification


def _run_validation_command(
    command: tuple[str, ...], workspace_path: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=workspace_path, capture_output=True, check=False, text=True)


@dataclass(frozen=True, slots=True)
class Stage1ValidationResult:
    """Durable outcome and redacted artifact for one validation command set."""

    passed: bool
    artifact_digest: Digest


CommandRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class Stage1ValidationExecutor:
    """Run manifest-bound validation only after its node is durable."""

    runtime: CoordinatorRuntime
    artifact_store: ArtifactStore
    command_runner: CommandRunner = _run_validation_command

    def validate(
        self,
        *,
        dispatch: WorkflowNodeDispatch,
        commands: tuple[tuple[str, ...], ...],
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1ValidationResult:
        """Persist validation intent, run commands, and retain redacted output."""
        if not commands or any(
            not command or any(not argument for argument in command) for command in commands
        ):
            raise InvalidInputError("Stage 1 validation requires non-empty commands")
        epoch = self.runtime.register_node(
            dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
        )
        results = tuple(
            self.command_runner(command, dispatch.request.workspace_path) for command in commands
        )
        artifact_digest = self.artifact_store.publish_bytes(
            _redacted_validation_report(commands, results),
            media_type="application/json",
        )
        passed = all(result.returncode == 0 for result in results)
        self.runtime.complete_node(
            run_id=dispatch.request.run_id,
            node_id=dispatch.request.node_id,
            epoch=epoch.epoch,
            now=now,
            outcome="passed" if passed else "failed",
            extra={
                "validation_artifact_digest": str(artifact_digest),
                "validation_command_count": len(commands),
            },
        )
        return Stage1ValidationResult(passed=passed, artifact_digest=artifact_digest)


@dataclass(frozen=True, slots=True)
class Stage1ReviewResult:
    """Durable routing decision from one independent review report."""

    outcome: ReviewOutcome


@dataclass(frozen=True, slots=True)
class Stage1ReviewExecutor:
    """Persist independent review decisions through the shared runtime."""

    runtime: CoordinatorRuntime

    def review(
        self,
        *,
        dispatch: WorkflowNodeDispatch,
        report: ReviewReport,
        repair_attempt: int,
        repair_limit: int,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stage1ReviewResult:
        """Record a review decision after its human-owned node is durable."""
        outcome = route_review(report, repair_attempt=repair_attempt, repair_limit=repair_limit)
        epoch = (
            self.runtime.register_node(
                dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
            )
            if repair_attempt == 0
            else self.runtime.retry_workflow_node(
                dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
            )
        )
        self.runtime.await_human_node(
            run_id=dispatch.request.run_id,
            node_id=dispatch.request.node_id,
            epoch=epoch.epoch,
            now=now,
            reason="independent review required",
        )
        self.runtime.resolve_human_wait(
            run_id=dispatch.request.run_id,
            node_id=dispatch.request.node_id,
            epoch=epoch.epoch,
            now=now,
            outcome="passed",
            extra={
                "review_outcome": outcome.value,
                "reviewer": report.reviewer,
                "producer": report.producer,
                "finding_ids": [finding.finding_id for finding in report.findings],
            },
        )
        return Stage1ReviewResult(outcome=outcome)


def _redacted_validation_report(
    commands: tuple[tuple[str, ...], ...],
    results: tuple[subprocess.CompletedProcess[str], ...],
) -> bytes:
    return json.dumps(
        {
            "commands": [
                {
                    "arguments": [redact_credential_shaped_text(argument) for argument in command],
                    "returncode": result.returncode,
                    "output": redact_credential_shaped_text(result.stdout + result.stderr),
                }
                for command, result in zip(commands, results, strict=True)
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _qualification_details(
    snapshot: WorkLedgerSnapshot, qualification: WorkQualification
) -> dict[str, object]:
    return {
        "external_reference": str(snapshot.work_item.external_reference),
        "source_revision": qualification.source_revision,
        "source_digest": qualification.source_digest,
        "readiness": qualification.readiness.value,
    }


__all__ = [
    "Stage1QualificationExecutor",
    "Stage1ReviewExecutor",
    "Stage1ReviewResult",
    "Stage1ValidationExecutor",
    "Stage1ValidationResult",
]
