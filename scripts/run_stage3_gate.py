#!/usr/bin/env python3
"""Live Stage 3 gate: incident to hotfix, deployed, observed, rolled
back, and observed restored -- against the real controlled local
service fixture.

Unlike Stage 2's live GitHub/PyPI gate, this touches only a local
process and no external credential, so it is not opt-in gated: it runs
as part of ordinary per-PR and full gates. The narrative:

1. A "production" baseline (revision v1, an off-by-one increment bug)
   is already running on the controlled local service.
2. An incident is ingested, classified, and bound to that release
   lineage.
3. Reproduction is attempted for real (a live HTTP call against v1)
   before the incident is ever treated as confirmed.
4. A hotfix worktree is created at v1, the minimal repair is applied
   and committed, and non-vacuous regression evidence is produced by
   running the same real check against both revisions.
5. The hotfix is reviewed, then deployed under an independent,
   policy-approved, short-lived grant -- deliberately configured with a
   health-degraded defect for this run, to force the rollback
   threshold.
6. The deployment is observed for real; the unhealthy result begins
   rollback under a second, independently approved grant.
7. Rollback is executed for real and the local service's live
   ``/version`` endpoint is polled to confirm the prior revision is
   genuinely restored -- not asserted from local state alone.
8. Separate follow-up work is recorded, never expanding the emergency
   PR's own scope.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import socket
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.local import LocalValidation
from enginery.adapters.local_service import (
    LocalServiceBuild,
    LocalServiceDeploymentAdapter,
    build_local_service_artifact,
)
from enginery.application.delivery_ports import DeploymentRequest
from enginery.domain.errors import EngineryError
from enginery.domain.ids import OperationId, RunId
from enginery.domain.incident import (
    IncidentSeverity,
    IncidentState,
    ReleaseLineage,
    ReproductionOutcome,
    ReproductionRecord,
)
from enginery.domain.policy_decision import PolicyAction
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.incidents.hotfix import (
    HotfixRepair,
    apply_repair,
    create_hotfix_worktree,
    prove_non_vacuous_regression,
    remove_hotfix_worktree,
)
from enginery.incidents.service import IncidentService
from enginery.ledger.service import LedgerService
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema
from enginery.workflows.review import ReviewOutcome, ReviewReport, route_review

_APP_SCRIPT = (
    Path(__file__).resolve().parent.parent / "fixtures" / "enginery-stage3-local-service" / "app.py"
)
_BUGGY_APP = "def add(a, b):\n    return a + b + 1\n"
_FIXED_APP = "def add(a, b):\n    return a + b\n"
_CHECK_COMMAND = ("python3", "-c", "exec(open('app.py').read()); assert add(2, 3) == 5")
_HUMAN = AuthorityPrincipal(
    id="operator-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_REQUESTING_PRINCIPAL_ID = "incident-workflow"


@dataclass(frozen=True, slots=True)
class Stage3GateReport:
    incident_id: str
    release_lineage: ReleaseLineage
    regression_non_vacuous: bool
    deployed_revision: str
    observed_before_rollback: str
    rolled_back: bool
    restored_revision: str
    authority_record_count: int
    follow_up_work_item_id: str
    final_state: str


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def _check_reproduction(target: str) -> ReproductionRecord:
    body = json.dumps({"value": 2}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{target}/increment", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        observed = json.loads(response.read())["result"]
    if observed != 3:
        return ReproductionRecord(
            outcome=ReproductionOutcome.REPRODUCED,
            detail=f"increment(2) returned {observed}, expected 3",
        )
    return ReproductionRecord(
        outcome=ReproductionOutcome.UNAVAILABLE, detail="increment(2) returned the correct result"
    )


def run_gate() -> Stage3GateReport:
    """Run the full live Stage 3 gate and return its evidence report.

    Raises :class:`EngineryError` on any deviation from the expected
    narrative -- the gate never reports success from a partial or
    ambiguous outcome.
    """
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        repo = tmp / "hotfix-repo"
        repo.mkdir()
        _git("init", cwd=repo)
        _git("config", "user.email", "stage3-gate@example.invalid", cwd=repo)
        _git("config", "user.name", "Stage 3 Gate", cwd=repo)
        (repo / "app.py").write_text(_BUGGY_APP, encoding="utf-8")
        _git("add", "app.py", cwd=repo)
        _git("commit", "-m", "v1: buggy add()", cwd=repo)
        base_revision = _git("rev-parse", "HEAD", cwd=repo)

        adapter = LocalServiceDeploymentAdapter(
            artifacts_root=tmp / "artifacts",
            state_root=tmp / "state",
            app_script=_APP_SCRIPT,
            ready_attempts=50,
            ready_interval_seconds=0.05,
        )
        target = f"127.0.0.1:{_free_port()}"
        registry = ApprovalRegistry(registered_humans=(_HUMAN,))
        policy = PolicyEvaluator(policy_version="1.0.0", approval_registry=registry)
        workspace = None
        ledger = LedgerService.open(tmp / "ledger.db")
        try:
            service = IncidentService(ledger=ledger, deployment=adapter, policy=policy)

            # The already-broken "production" baseline the incident responds to.
            v1_artifact = build_local_service_artifact(
                LocalServiceBuild(version=base_revision, defect_mode="increment_off_by_one"),
                artifacts_root=adapter.artifacts_root,
            )
            adapter.deploy(
                DeploymentRequest(
                    run_id=RunId("stage3-gate"),
                    artifact=v1_artifact,
                    target=target,
                    operation_id=OperationId(value="0" * 63 + "1"),
                )
            )

            incident = service.ingest(
                source_provider="stage3-gate",
                external_reference="checkout-increment-bug",
                source_snapshot_reference=base_revision,
                title="checkout increment returns the wrong result",
                objective="restore correct checkout increment behavior",
                acceptance_criteria=("increment(2) returns 3",),
                repository_targets=("stage3-fixture/checkout",),
                severity=IncidentSeverity.HIGH,
                summary="checkout increment endpoint is off by one",
            )
            service.classify(incident.id)
            lineage = ReleaseLineage(service=target, affected_revision=base_revision)
            service.bind_release_lineage(incident.id, lineage)

            service.begin_reproduction(incident.id)
            reproduced = service.attempt_reproduction(
                incident.id, check=lambda: _check_reproduction(target)
            )
            if reproduced.state is not IncidentState.REMEDIATING:
                raise EngineryError("reproduction did not confirm the incident")

            worktree_root = tmp / "hotfix-workspace"
            workspace = create_hotfix_worktree(
                repository=repo,
                base_revision=base_revision,
                branch="hotfix/checkout-increment",
                worktree_root=worktree_root,
            )
            repair = HotfixRepair(
                file_path="app.py", content=_FIXED_APP, commit_message="fix off-by-one in add()"
            )
            repaired_revision = apply_repair(workspace, repair)
            evidence = prove_non_vacuous_regression(
                LocalValidation(),
                run_id=RunId("stage3-gate"),
                workspace=workspace,
                command=_CHECK_COMMAND,
                repaired_revision=repaired_revision,
            )
            if not evidence.is_non_vacuous:
                raise EngineryError("regression evidence is vacuous; refusing to deploy")

            review = route_review(
                ReviewReport(
                    producer="hotfix-worker", reviewer="independent-reviewer", findings=()
                ),
                repair_attempt=0,
                repair_limit=1,
            )
            if review is not ReviewOutcome.APPROVED:
                raise EngineryError("hotfix review did not approve")

            service.begin_deployment(incident.id)
            # Deliberately health-degraded for this run to exercise the
            # rollback threshold -- the underlying code fix above is real
            # and separately proven non-vacuous.
            v2_artifact = build_local_service_artifact(
                LocalServiceBuild(version=repaired_revision, defect_mode="health_degraded"),
                artifacts_root=adapter.artifacts_root,
            )
            registry.record_approval(
                ApprovalSchema(
                    action=PolicyAction.DEPLOYMENT_EXECUTE,
                    risk_class=incident.risk_class,
                    target_resource=target,
                    diff_or_artifact_digest=str(v2_artifact.digest),
                    requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                ),
                (_HUMAN,),
                decided_at=datetime.now(UTC),
            )
            receipt = service.execute_deployment(
                incident.id,
                artifact=v2_artifact,
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=datetime.now(UTC),
            )
            deployed = adapter.observe(target)
            if deployed.revision != repaired_revision:
                raise EngineryError("deployed revision does not match the hotfix revision")

            service.begin_observation(incident.id)
            observation = adapter.observe(target, attempts=5, interval_seconds=0.05)
            if observation.healthy:
                raise EngineryError("expected the deployed revision to be unhealthy in this run")
            resolved = service.resolve_observation(incident.id, healthy=observation.healthy)
            if resolved.state is not IncidentState.ROLLING_BACK:
                raise EngineryError("unhealthy observation did not begin rollback")

            registry.record_approval(
                ApprovalSchema(
                    action=PolicyAction.DEPLOYMENT_ROLLBACK,
                    risk_class=incident.risk_class,
                    target_resource=target,
                    diff_or_artifact_digest=str(receipt.artifact_digest),
                    requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                ),
                (_HUMAN,),
                decided_at=datetime.now(UTC),
            )
            final_incident = service.execute_rollback(
                incident.id,
                receipt=receipt,
                requesting_principal_id=_REQUESTING_PRINCIPAL_ID,
                now=datetime.now(UTC),
            )
            if final_incident.state is not IncidentState.ROLLED_BACK:
                raise EngineryError("rollback did not reach the rolled_back terminal state")

            restored = adapter.observe(target)
            if restored.revision != base_revision:
                raise EngineryError("rollback did not restore the prior revision")

            follow_up = service.record_follow_up(
                incident.id,
                title="investigate deployment health-check degradation on hotfix rollout",
                objective="root-cause why the hotfix build failed its deployment health check",
                acceptance_criteria=("root cause documented and a remediation plan filed",),
                repository_targets=("stage3-fixture/checkout",),
            )

            return Stage3GateReport(
                incident_id=str(incident.id),
                release_lineage=lineage,
                regression_non_vacuous=evidence.is_non_vacuous,
                deployed_revision=repaired_revision,
                observed_before_rollback=observation.revision or "unknown",
                rolled_back=True,
                restored_revision=restored.revision or "unknown",
                authority_record_count=len(service.list_authority_records(incident.id)),
                follow_up_work_item_id=str(follow_up.id),
                final_state=final_incident.state.value,
            )
        finally:
            state = adapter._read_state(target)
            if state is not None:
                adapter._stop(state["current"])
                if state.get("previous") is not None:
                    adapter._stop(state["previous"])
            if workspace is not None and workspace.root.exists():
                remove_hotfix_worktree(repository=repo, workspace=workspace)
            ledger.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live Stage 3 gate: incident to hotfix, deploy, observe, rollback, restore."
    )
    parser.add_argument("--fixture", choices=("local-http-service",), default="local-http-service")
    parser.parse_args(argv)

    report = run_gate()
    print(json.dumps(dataclasses.asdict(report), indent=2, default=str))
    print(f"Stage 3 gate PASSED: incident {report.incident_id} reached {report.final_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
