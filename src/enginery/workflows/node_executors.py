"""Coordinator-owned Stage 1 deterministic node composition."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enginery.application.runner_ports import AttestedProcess
from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.application.worker_environment import (
    TRUSTED_LOCAL_WORKER_KEYS,
    build_worker_environment,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import HumanActionRequiredError, InvalidInputError
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


def run_sanitized_validation_command(
    command: tuple[str, ...], workspace_path: Path
) -> subprocess.CompletedProcess[str]:
    """Run a host-side validation command with an explicit environment, not the ambient one.

    Validation executes candidate-authored code. Run for a trusted-local run it still runs as the
    operator, but only with the variables the trusted-local worker itself is allowed: the
    operator's SCM, factory, broker, cloud and publishing credentials are not inherited.
    """
    return subprocess.run(
        command,
        cwd=workspace_path,
        capture_output=True,
        check=False,
        text=True,
        env=build_worker_environment(os.environ, TRUSTED_LOCAL_WORKER_KEYS),
    )


@dataclass(frozen=True, slots=True)
class Stage1ValidationResult:
    """Durable outcome and redacted artifact for one validation command set."""

    passed: bool
    artifact_digest: Digest


CommandRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]

# What the in-container wrapper reports when the command's executable cannot be started: exit 127
# from a shell, or exit 1 with the operating system's own words when it execs the command itself.
_NOT_FOUND_EXIT_CODE = 127
_START_FAILURE_PREFIXES = ("[Errno 2] ", "[Errno 13] ")
_PROBE_SCRIPT = 'command -v "$1" >/dev/null 2>&1'


@dataclass(frozen=True, slots=True)
class MissingToolProbingRunner:
    """Tell a command that could not start for want of its tool from a validation that failed.

    Isolated validation runs offline in a fixed image, so a project tool the image lacks cannot
    be run and never will be. When a command ends the way a missing executable ends, the same
    runner is asked inside the same container restrictions whether the executable exists
    (``command -v``). Only a confirmed absence is a hold naming the tool; a command that merely
    returned 127 itself, or failed for any other reason, is the validation failure it is.
    """

    runner: CommandRunner

    def __call__(
        self, command: tuple[str, ...], workspace_path: Path
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(command, workspace_path)
        if result.returncode == 0 or not _looks_unstartable(result):
            return result
        executable = command[0]
        if executable.startswith("-"):
            return result
        probe = self.runner(
            ("sh", "-c", _PROBE_SCRIPT, "enginery-probe", executable), workspace_path
        )
        if probe.returncode == 0:
            return result
        raise HumanActionRequiredError(
            f"the validation command's executable {executable!r} is not available in the pinned "
            "runner image, and isolated validation runs offline, so it cannot be installed: "
            "the run is held rather than reported as a failed validation. Name validation "
            "commands that run with the tools in the image (python3, node, git, uv, sh) or "
            "with what the repository itself carries",
            details={"tool": executable, "command": list(command)},
        )


def _looks_unstartable(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == _NOT_FOUND_EXIT_CODE:
        return True
    return result.returncode == 1 and str(result.stdout).lstrip().startswith(
        _START_FAILURE_PREFIXES
    )


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
        working_directory: Path | None = None,
        validated_revision: str | None = None,
    ) -> Stage1ValidationResult:
        """Persist validation intent, run commands, and retain redacted output.

        Commands run in ``working_directory`` when given and otherwise in the node's own
        workspace. ``validated_revision`` records the exact revision the commands ran against.
        """
        if not commands or any(
            not command or any(not argument for argument in command) for command in commands
        ):
            raise InvalidInputError("Stage 1 validation requires non-empty commands")
        epoch = self.runtime.register_node(
            dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
        )
        try:
            results = tuple(
                self.command_runner(command, working_directory or dispatch.request.workspace_path)
                for command in commands
            )
        except HumanActionRequiredError as hold:
            # A hold is not a verdict, but it is not a decision a human can take either: the
            # request is digest-bound, so the commands cannot change, and approving a validation
            # that never ran would let an unvalidated branch reach review and publication. The
            # node is recorded as blocked with the reason on it, so the run neither looks merely
            # queued nor offers an approval; only a new run with commands the image can run
            # proceeds.
            self.runtime.complete_node(
                run_id=dispatch.request.run_id,
                node_id=dispatch.request.node_id,
                epoch=epoch.epoch,
                now=now,
                outcome="blocked",
                extra={"blocked_reason": str(hold), **hold.details},
            )
            raise
        artifact_digest = self.artifact_store.publish_bytes(
            _redacted_validation_report(commands, results),
            media_type="application/json",
        )
        passed = all(result.returncode == 0 for result in results)
        # An isolated validation names the retained receipt each command ran under; a
        # validation without a runner (trusted-local, legacy) records none.
        receipt_digests = [
            str(result.runner_receipt.receipt_digest)
            for result in results
            if isinstance(result, AttestedProcess)
        ]
        self.runtime.complete_node(
            run_id=dispatch.request.run_id,
            node_id=dispatch.request.node_id,
            epoch=epoch.epoch,
            now=now,
            outcome="passed" if passed else "failed",
            extra={
                "validation_artifact_digest": str(artifact_digest),
                "validation_command_count": len(commands),
                **(
                    {} if validated_revision is None else {"validated_revision": validated_revision}
                ),
                **({"validation_runner_receipts": receipt_digests} if receipt_digests else {}),
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
        execution_selection_digest: Digest | None,
        now: datetime,
        heartbeat_window: timedelta,
        packet_feedback: bool = False,
        feedback_text: str | None = None,
    ) -> Stage1ReviewResult:
        """Record a review decision after its human-owned node is durable."""
        outcome = route_review(report, repair_attempt=repair_attempt, repair_limit=repair_limit)
        epoch = (
            self.runtime.register_node(
                dispatch=dispatch, now=now, heartbeat_window=heartbeat_window
            )
            if repair_attempt == 0 or packet_feedback
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
                "actionable_finding_ids": [
                    finding.finding_id for finding in report.findings if finding.actionable
                ],
                **(
                    {}
                    if feedback_text is None
                    else {"feedback_text": redact_credential_shaped_text(feedback_text)}
                ),
                **(
                    {}
                    if execution_selection_digest is None
                    else {"execution_selection_digest": str(execution_selection_digest)}
                ),
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
                    **(
                        {"runner_receipt": result.runner_receipt.to_state()}
                        if isinstance(result, AttestedProcess)
                        else {}
                    ),
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
