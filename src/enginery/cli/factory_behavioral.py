"""Bind trusted observer execution to the existing Stage 1 run and ledger."""

from __future__ import annotations

import re
import shlex
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from enginery.adapters.checkout_identity import host_environment
from enginery.application.behavioral_calibration import AcceptanceInputs
from enginery.application.behavioral_checks import FAULT_CLASSES, BehavioralCheck, FaultClass
from enginery.cli.config_binding import behavioral_checks_path, read_behavioral_checks
from enginery.cli.config_locations import private_state_directory, read_user_layer
from enginery.cli.factory_collect import database_path, ensure_private_directory
from enginery.cli.runner_wiring import DriftedRunnerWiring, IsolatedWiring, isolated_wiring
from enginery.domain.behavioral_evidence import EnvironmentFingerprint, EvaluatorIdentity
from enginery.domain.digests import Digest
from enginery.domain.errors import HumanActionRequiredError, InvalidInputError
from enginery.domain.ids import RunId
from enginery.domain.node_attempt import EvidenceResult
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE, CoordinatorRuntime
from enginery.engine.task_repository import (
    intake_path_for,
    materialize_task_local_repository,
    resolve_commit,
)
from enginery.evidence.behavioral import calibration_holds
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.behavioral_acceptance import BehavioralAcceptanceService
from enginery.workflows.run_lifecycle import Stage1RunRequest, Stage1RunService


class _AcceptanceArguments(TypedDict):
    checks: tuple[BehavioralCheck, ...]
    cwd: Path
    source_revision: str
    base_revision: str
    head_revision: str
    configuration_snapshot_digest: Digest
    evaluator: EvaluatorIdentity
    environment: EnvironmentFingerprint


def _service(request: Stage1RunRequest) -> BehavioralAcceptanceService:
    binding = read_behavioral_checks(request)
    if binding is None:
        raise InvalidInputError("historical request has no independently bound behavioral checks")
    path = behavioral_checks_path(request)
    assert path is not None
    ensure_private_directory(path.parent, what="behavioral evaluator custody")
    ensure_private_directory(
        request.execution_configuration.artifact_root, what="evidence artifacts"
    )
    user = read_user_layer()
    programs = None if user is None else user.operator_executables
    wiring = isolated_wiring(request, create_directories=True)
    if isinstance(wiring, DriftedRunnerWiring):
        raise HumanActionRequiredError(
            "behavioral_observer_requires_isolation: restore the run's bound runner profile"
        )
    if isinstance(wiring, IsolatedWiring):
        admission = wiring.admission.admit(request.runner)
        if admission.hold is not None:
            raise HumanActionRequiredError(
                admission.hold.message, details=dict(admission.hold.details)
            )
    return BehavioralAcceptanceService(
        ArtifactStore(request.execution_configuration.artifact_root),
        path.parent,
        browser_executable=None if programs is None else programs.browser,
        browser_expected_digest=None if programs is None else programs.browser_digest,
        isolated=request.runner.isolated_mode,
        command_runner=None if wiring is None else wiring.validation,
        calibration_python="python3" if request.runner.isolated_mode else sys.executable,
    )


def execute_behavioral(
    ledger: LedgerService,
    request: Stage1RunRequest,
    *,
    head_revision: str,
    calibrate: bool = False,
    without_faults: tuple[str, ...] = (),
    calibrate_when_required: bool = False,
) -> dict[str, object]:
    binding = read_behavioral_checks(request)
    if binding is None:
        raise InvalidInputError("historical request has no behavioral check custody")
    service = _service(request)
    root = private_state_directory(request.repository_path) / "evaluator" / "checkouts"
    ensure_private_directory(root, what="behavioral source checkouts")
    source = (
        intake_path_for(request.workspace_path)
        if request.runner.isolated_mode
        else request.workspace_path
    )
    with tempfile.TemporaryDirectory(prefix="observe-", dir=root) as temporary:
        checkout = Path(temporary) / "candidate"
        materialize_task_local_repository(
            repository_path=source, workspace_path=checkout, base_revision=head_revision
        )
        arguments: _AcceptanceArguments = {
            "checks": binding.checks,
            "cwd": checkout,
            "source_revision": request.work_snapshot.source_revision,
            "base_revision": request.run.base_revision,
            "head_revision": head_revision,
            "configuration_snapshot_digest": request.run.configuration_snapshot_digest,
            "evaluator": EvaluatorIdentity(binding.assertion_set_digest, binding.evaluator_version),
            "environment": host_environment(),
        }
        if calibrate:
            calibration = service.calibrate(
                **arguments,
                inject_faults=tuple(
                    cast(FaultClass, fault)
                    for fault in FAULT_CLASSES
                    if fault not in without_faults
                ),
            )
            holds = calibration_holds(
                binding.checks,
                AcceptanceInputs(
                    source_revision=arguments["source_revision"],
                    base_revision=arguments["base_revision"],
                    head_revision=head_revision,
                    build=calibration.receipt.build,
                    configuration_snapshot_digest=arguments["configuration_snapshot_digest"],
                    evaluator=arguments["evaluator"],
                    environment=arguments["environment"],
                ),
                calibration,
            )
            state: dict[str, object] = {
                "calibration_receipt_digest": str(calibration.receipt_digest),
                "calibration": calibration.to_state(),
                "head_revision": head_revision,
                "holds": list(holds),
                "qualified": not holds,
            }
        else:
            result = service.run(**arguments)
            if (
                calibrate_when_required
                and result.result is not EvidenceResult.FAIL
                and any(hold.startswith("behavioral_calibration_") for hold in result.holds)
            ):
                service.calibrate(**arguments)
                result = service.run(**arguments)
            state = {
                "receipt_digest": str(result.receipt_digest),
                "observation_receipt_digests": [
                    str(item) for item in result.observation_receipt_digests
                ],
                "result": result.result.value,
                "holds": list(result.holds),
                "head_revision": head_revision,
            }
    state["configuration_snapshot_digest"] = str(request.run.configuration_snapshot_digest)
    aggregate = "behavioral_calibration" if calibrate else "behavioral_acceptance"
    old = ledger.read_projection(aggregate_type=aggregate, aggregate_id=str(request.run.id))
    ledger.append(
        AppendCommand(
            correlation_id=f"{aggregate}:{request.run.id}:{datetime.now(UTC).isoformat()}",
            events=(
                EventWrite(
                    aggregate_type=aggregate,
                    aggregate_id=str(request.run.id),
                    expected_version=0 if old is None else old.aggregate_version,
                    event_type=aggregate + "_recorded",
                    schema_version=1,
                    payload=state,
                ),
            ),
        )
    )
    return {"run_id": str(request.run.id), **state}


def verify_for_stage1(
    ledger: LedgerService, request: Stage1RunRequest, head: str, *, owner: str = "factory"
) -> Digest:
    # Stage 1 owns calibration for its current build; no sidecar CLI result is required.
    state = execute_behavioral(ledger, request, head_revision=head, calibrate_when_required=True)
    if state["result"] != EvidenceResult.PASS.value:
        failed = state["result"] == EvidenceResult.FAIL.value
        reason = "behavioral_acceptance_failed" if failed else "behavioral_acceptance_indeterminate"
        database = str(ledger.connection.execute("PRAGMA database_list").fetchone()[2])
        retry = shlex.join(
            (
                "enginery",
                "engine",
                "next",
                "--database",
                database,
                "--owner",
                owner,
                "--run-id",
                str(request.run.id),
                "--advance",
            )
        )
        raise HumanActionRequiredError(
            reason + ": " + "; ".join(cast(list[str], state["holds"])),
            details={
                "reason": reason,
                "findings": tuple(cast(list[str], state["holds"])) if failed else (),
                "repair": "Repair the named observer infrastructure, then "
                + retry
                + "; to explicitly refresh calibration, run "
                + shlex.join(
                    (
                        "enginery",
                        "calibrate",
                        str(request.run.id),
                        "--project",
                        str(request.repository_path),
                        "--database",
                        database,
                        "--owner",
                        owner,
                    )
                )
                if not failed
                else "Start the bounded repair attempt with " + retry,
            },
        )
    algorithm, value = str(state["receipt_digest"]).split(":", 1)
    return Digest(algorithm, value)


def run_command(
    project: Path,
    run_id: str,
    *,
    calibrate: bool,
    without_faults: tuple[str, ...] = (),
    database: Path | None = None,
    owner: str = "factory",
) -> dict[str, object]:
    with LedgerService.open(database if database is not None else database_path(project)) as ledger:
        request = (
            Stage1RunService(runtime=CoordinatorRuntime(ledger, owner=owner), ledger=ledger)
            .read(RunId(run_id))
            .request
        )
        implementation = ledger.read_projection(
            aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE, aggregate_id=f"{run_id}:implement"
        )
        attempt_id = None if implementation is None else implementation.state.get("attempt_id")
        attempt = (
            ledger.read_projection(aggregate_type="node_attempt", aggregate_id=attempt_id)
            if isinstance(attempt_id, str)
            else None
        )
        result = None if attempt is None else attempt.state.get("result")
        head = None if not isinstance(result, Mapping) else result.get("head_revision")
        if (
            not isinstance(head, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None
        ):
            raise HumanActionRequiredError(
                "behavioral check requires a verified implementation attempt head"
            )
        if (
            not request.runner.isolated_mode
            and resolve_commit(request.workspace_path, "HEAD") != head
        ):
            raise HumanActionRequiredError("behavioral_build_mismatch: workspace HEAD changed")
        return execute_behavioral(
            ledger, request, head_revision=head, calibrate=calibrate, without_faults=without_faults
        )
