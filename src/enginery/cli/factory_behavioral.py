"""Bind trusted observer execution to the existing Stage 1 run and ledger."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from enginery.adapters.checkout_identity import host_environment
from enginery.application.behavioral_calibration import AcceptanceInputs
from enginery.application.behavioral_checks import FAULT_CLASSES, BehavioralCheck, FaultClass
from enginery.cli.config_binding import behavioral_checks_path, read_behavioral_checks
from enginery.cli.config_locations import private_state_directory, read_user_layer
from enginery.cli.factory_collect import database_path, ensure_private_directory
from enginery.domain.behavioral_evidence import EnvironmentFingerprint, EvaluatorIdentity
from enginery.domain.digests import Digest
from enginery.domain.errors import HumanActionRequiredError, InvalidInputError
from enginery.domain.ids import RunId
from enginery.domain.node_attempt import EvidenceResult
from enginery.engine.runtime import CoordinatorRuntime
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
from enginery.workflows.stage1 import Stage1RunRequest, Stage1RunService


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
    ensure_private_directory(request.execution_configuration.artifact_root, what="evidence artifacts")
    user = read_user_layer()
    programs = None if user is None else user.operator_executables
    return BehavioralAcceptanceService(
        ArtifactStore(request.execution_configuration.artifact_root), path.parent,
        browser_executable=None if programs is None else programs.browser,
        browser_expected_digest=None if programs is None else programs.browser_digest,
    )


def execute_behavioral(
    ledger: LedgerService, request: Stage1RunRequest, *, head_revision: str, calibrate: bool = False,
    without_faults: tuple[str, ...] = (),
) -> dict[str, object]:
    binding = read_behavioral_checks(request)
    if binding is None:
        raise InvalidInputError("historical request has no behavioral check custody")
    service = _service(request)
    root = private_state_directory(request.repository_path) / "evaluator" / "checkouts"
    ensure_private_directory(root, what="behavioral source checkouts")
    source = (
        intake_path_for(request.workspace_path)
        if request.runner.isolated_mode else request.workspace_path
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
                    cast(FaultClass, fault) for fault in FAULT_CLASSES if fault not in without_faults
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
                    evaluator=arguments["evaluator"], environment=arguments["environment"],
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
            state = {
                "receipt_digest": str(result.receipt_digest),
                "observation_receipt_digests": [str(item) for item in result.observation_receipt_digests],
                "result": result.result.value,
                "holds": list(result.holds),
                "head_revision": head_revision,
            }
    state["configuration_snapshot_digest"] = str(request.run.configuration_snapshot_digest)
    aggregate = "behavioral_calibration" if calibrate else "behavioral_acceptance"
    old = ledger.read_projection(aggregate_type=aggregate, aggregate_id=str(request.run.id))
    ledger.append(AppendCommand(
        correlation_id=f"{aggregate}:{request.run.id}:{datetime.now(UTC).isoformat()}",
        events=(EventWrite(
            aggregate_type=aggregate, aggregate_id=str(request.run.id),
            expected_version=0 if old is None else old.aggregate_version,
            event_type=aggregate + "_recorded", schema_version=1, payload=state,
        ),),
    ))
    return {"run_id": str(request.run.id), **state}


def verify_for_stage1(ledger: LedgerService, request: Stage1RunRequest, head: str) -> Digest:
    state = execute_behavioral(ledger, request, head_revision=head)
    if state["result"] != EvidenceResult.PASS.value:
        raise HumanActionRequiredError(
            "behavioral_acceptance_indeterminate: "
            + "; ".join(cast(list[str], state["holds"])),
            details={"repair": f"enginery factory calibrate {request.run.id} --project {request.repository_path}"},
        )
    algorithm, value = str(state["receipt_digest"]).split(":", 1)
    return Digest(algorithm, value)


def run_command(
    project: Path, run_id: str, *, calibrate: bool, without_faults: tuple[str, ...] = ()
) -> dict[str, object]:
    with LedgerService.open(database_path(project)) as ledger:
        request = Stage1RunService(
            runtime=CoordinatorRuntime(ledger, owner="factory"), ledger=ledger
        ).read(RunId(run_id)).request
        source = intake_path_for(request.workspace_path) if request.runner.isolated_mode else request.workspace_path
        head = resolve_commit(source, "HEAD")
        return execute_behavioral(
            ledger, request, head_revision=head, calibrate=calibrate, without_faults=without_faults
        )
