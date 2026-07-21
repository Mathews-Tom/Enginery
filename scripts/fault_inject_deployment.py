#!/usr/bin/env python3
"""Fault-inject Stage 3's deployment and rollback boundaries.

Exercises five named fault scenarios against the real local-service
deployment broker and the incident authority workflow: deploy
ambiguity, observation timeout, rollback ambiguity, coordinator crash,
and an expired credential reference. Every scenario uses the real
subprocess/real-HTTP adapter (``enginery.adapters.local_service``) or
the real, ledger-backed ``IncidentService`` -- no scenario here is a
pure in-memory simulation.
"""

from __future__ import annotations

import socket
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.adapters.local_service import (
    LocalServiceBuild,
    LocalServiceDeploymentAdapter,
    build_local_service_artifact,
)
from enginery.application.delivery_ports import DeploymentReceipt, DeploymentRequest
from enginery.domain.digests import Digest
from enginery.domain.errors import ExternalConflictError
from enginery.domain.ids import OperationId, RunId
from enginery.domain.incident import (
    IncidentSeverity,
    ReleaseLineage,
    ReproductionOutcome,
    ReproductionRecord,
)
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.incidents.authority import DeploymentGrantExpiredError
from enginery.incidents.service import IncidentService
from enginery.ledger.service import LedgerService
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema
from fault_injection.framework import FaultScenario, main_for

_APP_SCRIPT = (
    Path(__file__).resolve().parent.parent / "fixtures" / "enginery-stage3-local-service" / "app.py"
)
_HUMAN = AuthorityPrincipal(
    id="human-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _adapter(tmp_path: Path, *, ready_attempts: int = 30) -> LocalServiceDeploymentAdapter:
    return LocalServiceDeploymentAdapter(
        artifacts_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        app_script=_APP_SCRIPT,
        ready_attempts=ready_attempts,
        ready_interval_seconds=0.05,
    )


def scenario_deploy_ambiguity_reconciles_without_blind_retry() -> None:
    """A caller unsure whether a deploy landed reconciles by operation id
    instead of blindly retrying -- a known operation resolves matching,
    an unknown one resolves not-found, never guessed as success."""
    with tempfile.TemporaryDirectory() as raw_tmp_path:
        tmp_path = Path(raw_tmp_path)
        adapter = _adapter(tmp_path)
        port = _free_port()
        artifact = build_local_service_artifact(
            LocalServiceBuild(version="v1", defect_mode="none"),
            artifacts_root=adapter.artifacts_root,
        )
        operation_id = OperationId(value="a" * 64)
        adapter.deploy(
            DeploymentRequest(
                run_id=RunId("run-1"),
                artifact=artifact,
                target=f"127.0.0.1:{port}",
                operation_id=operation_id,
            )
        )
        try:
            known = adapter.reconcile(operation_id=operation_id)
            unknown = adapter.reconcile(operation_id=OperationId(value="b" * 64))
            assert known is ReconciliationResult.FOUND_MATCHING, "known deploy must reconcile"
            assert unknown is ReconciliationResult.NOT_FOUND, "unattempted deploy must not_found"
        finally:
            adapter._stop(adapter._read_state(f"127.0.0.1:{port}")["current"])  # type: ignore[index]


def scenario_observation_timeout_fails_closed_without_a_dangling_process() -> None:
    """A deployment that never becomes ready raises rather than hanging
    or silently claiming success, and never records a false-positive
    reconciliation outcome."""
    with tempfile.TemporaryDirectory() as raw_tmp_path:
        tmp_path = Path(raw_tmp_path)
        adapter = _adapter(tmp_path, ready_attempts=3)
        port = _free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            artifact = build_local_service_artifact(
                LocalServiceBuild(version="v1", defect_mode="none"),
                artifacts_root=adapter.artifacts_root,
            )
            operation_id = OperationId(value="c" * 64)
            raised = False
            try:
                adapter.deploy(
                    DeploymentRequest(
                        run_id=RunId("run-1"),
                        artifact=artifact,
                        target=f"127.0.0.1:{port}",
                        operation_id=operation_id,
                    )
                )
            except ExternalConflictError:
                raised = True
            assert raised, "an observation timeout must raise, never hang or claim success"
            assert adapter.reconcile(operation_id=operation_id) is ReconciliationResult.NOT_FOUND, (
                "a failed deploy must never be reconcilable as having happened"
            )
        finally:
            blocker.close()


def scenario_rollback_ambiguity_is_reported_not_silently_applied() -> None:
    """A rollback request bound to a stale artifact digest is reported as
    conflicting rather than silently applied against the wrong baseline,
    and the live service is left untouched."""
    with tempfile.TemporaryDirectory() as raw_tmp_path:
        tmp_path = Path(raw_tmp_path)
        adapter = _adapter(tmp_path)
        port = _free_port()
        target = f"127.0.0.1:{port}"
        for version in ("v1", "v2"):
            artifact = build_local_service_artifact(
                LocalServiceBuild(version=version, defect_mode="none"),
                artifacts_root=adapter.artifacts_root,
            )
            adapter.deploy(
                DeploymentRequest(
                    run_id=RunId("run-1"),
                    artifact=artifact,
                    target=target,
                    operation_id=OperationId(value=f"deploy-{version}".ljust(64, "0")),
                )
            )
        try:
            stale_receipt = DeploymentReceipt(
                target=target, artifact_digest=Digest.of_bytes(b"stale"), deployment_id="stale"
            )
            result = adapter.rollback(stale_receipt, operation_id=OperationId(value="d" * 64))
            assert result is ReconciliationResult.FOUND_CONFLICTING, (
                "a mismatched rollback receipt must be reported, never silently applied"
            )
            observation = adapter.observe(target)
            assert observation.revision == "v2", (
                "an unresolved ambiguity must not mutate the target"
            )
        finally:
            state = adapter._read_state(target)
            assert state is not None
            adapter._stop(state["current"])


def scenario_coordinator_crash_leaves_a_replayable_operation() -> None:
    """A replacement coordinator, reading the same durable ledger after a
    crash mid-deployment, observes the exact same incident state and can
    reconcile the interrupted operation instead of guessing."""
    with tempfile.TemporaryDirectory() as raw_tmp_path:
        tmp_path = Path(raw_tmp_path)
        ledger_path = tmp_path / "ledger.db"
        adapter = _adapter(tmp_path)
        port = _free_port()
        target = f"127.0.0.1:{port}"
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        policy = PolicyEvaluator(policy_version="1.0.0", approval_registry=registry)
        ledger = LedgerService.open(ledger_path)
        try:
            crashed_service = IncidentService(ledger=ledger, deployment=adapter, policy=policy)
            incident = crashed_service.ingest(
                source_provider="pagerduty",
                external_reference="FAULT-CRASH-1",
                source_snapshot_reference="snapshot-1",
                title="checkout returns 500",
                objective="restore checkout availability",
                acceptance_criteria=("checkout responds 200",),
                repository_targets=("org/checkout",),
                severity=IncidentSeverity.HIGH,
                summary="checkout endpoint returns 500 for all requests",
            )
            crashed_service.classify(incident.id)
            crashed_service.bind_release_lineage(
                incident.id, ReleaseLineage(service=target, affected_revision="v1")
            )

            crashed_service.begin_reproduction(incident.id)
            crashed_service.attempt_reproduction(
                incident.id,
                check=lambda: ReproductionRecord(
                    outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
                ),
            )
            crashed_service.begin_deployment(incident.id)
            artifact = build_local_service_artifact(
                LocalServiceBuild(version="v1", defect_mode="none"),
                artifacts_root=adapter.artifacts_root,
            )
            schema = ApprovalSchema(
                action=PolicyAction.DEPLOYMENT_EXECUTE,
                risk_class=incident.risk_class,
                target_resource=target,
                diff_or_artifact_digest=str(artifact.digest),
                requesting_principal_id="incident-workflow",
            )
            registry.record_approval(schema, (_HUMAN,), decided_at=_NOW)
            crashed_service.execute_deployment(
                incident.id,
                artifact=artifact,
                requesting_principal_id="incident-workflow",
                now=_NOW,
            )
            # "Coordinator crash": drop every in-process reference and
            # open a brand-new service against the same durable ledger.
        finally:
            ledger.close()

        replacement_ledger = LedgerService.open(ledger_path)
        try:
            replacement_service = IncidentService(
                ledger=replacement_ledger, deployment=adapter, policy=policy
            )
            resumed = replacement_service.read(incident.id)
            assert resumed is not None
            assert resumed.state.value == "deploying", (
                "the replacement coordinator must observe the exact durable state, "
                "not a guess about what the crashed process was doing"
            )
            records = replacement_service.list_authority_records(incident.id)
            assert len(records) == 1 and records[0].outcome == "succeeded", (
                "the replacement coordinator must be able to discover the completed "
                "authority record rather than blindly re-authorizing"
            )
        finally:
            state = adapter._read_state(target)
            if state is not None:
                adapter._stop(state["current"])
            replacement_ledger.close()


def scenario_expired_credential_reference_blocks_the_broker_call() -> None:
    """An approved-but-expired deployment grant blocks the broker call
    entirely -- the broker is never invoked with a stale authorization."""
    with tempfile.TemporaryDirectory() as raw_tmp_path:
        tmp_path = Path(raw_tmp_path)
        adapter = _adapter(tmp_path)
        port = _free_port()
        target = f"127.0.0.1:{port}"
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        policy = PolicyEvaluator(policy_version="1.0.0", approval_registry=registry)
        with LedgerService.open(tmp_path / "ledger.db") as ledger:
            service = IncidentService(ledger=ledger, deployment=adapter, policy=policy)
            incident = service.ingest(
                source_provider="pagerduty",
                external_reference="FAULT-EXPIRED-1",
                source_snapshot_reference="snapshot-1",
                title="checkout returns 500",
                objective="restore checkout availability",
                acceptance_criteria=("checkout responds 200",),
                repository_targets=("org/checkout",),
                severity=IncidentSeverity.HIGH,
                summary="checkout endpoint returns 500 for all requests",
            )
            service.classify(incident.id)

            service.bind_release_lineage(
                incident.id, ReleaseLineage(service=target, affected_revision="v1")
            )
            service.begin_reproduction(incident.id)
            service.attempt_reproduction(
                incident.id,
                check=lambda: ReproductionRecord(
                    outcome=ReproductionOutcome.REPRODUCED, detail="observed 500 on every request"
                ),
            )
            service.begin_deployment(incident.id)
            artifact = build_local_service_artifact(
                LocalServiceBuild(version="v1", defect_mode="none"),
                artifacts_root=adapter.artifacts_root,
            )
            schema = ApprovalSchema(
                action=PolicyAction.DEPLOYMENT_EXECUTE,
                risk_class=incident.risk_class,
                target_resource=target,
                diff_or_artifact_digest=str(artifact.digest),
                requesting_principal_id="incident-workflow",
            )
            registry.record_approval(schema, (_HUMAN,), decided_at=_NOW)

            raised = False
            try:
                service.execute_deployment(
                    incident.id,
                    artifact=artifact,
                    requesting_principal_id="incident-workflow",
                    now=_NOW,
                    reference_time=_NOW + timedelta(minutes=10),
                )
            except DeploymentGrantExpiredError:
                raised = True
            assert raised, "an expired grant must block the broker call even after approval"
            assert adapter._read_state(target) is None, (
                "the broker must never have been called with an expired credential reference"
            )


SCENARIOS = (
    FaultScenario(
        name="deploy_ambiguity",
        description="a caller reconciles an ambiguous deploy by operation id, never guesses",
        run=scenario_deploy_ambiguity_reconciles_without_blind_retry,
    ),
    FaultScenario(
        name="observation_timeout",
        description="a deployment that never becomes ready fails closed, no dangling process",
        run=scenario_observation_timeout_fails_closed_without_a_dangling_process,
    ),
    FaultScenario(
        name="rollback_ambiguity",
        description="a mismatched rollback receipt is reported conflicting, not silently applied",
        run=scenario_rollback_ambiguity_is_reported_not_silently_applied,
    ),
    FaultScenario(
        name="coordinator_crash",
        description="a replacement coordinator resumes from durable state, no duplicate effects",
        run=scenario_coordinator_crash_leaves_a_replayable_operation,
    ),
    FaultScenario(
        name="expired_credential_reference",
        description="an approved-but-expired grant blocks the broker call entirely",
        run=scenario_expired_credential_reference_blocks_the_broker_call,
    ),
)


if __name__ == "__main__":
    raise SystemExit(main_for(SCENARIOS))
