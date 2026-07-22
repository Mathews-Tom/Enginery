"""End-to-end fixtures for ``enginery gate status --gate G4`` against a
real ledger: zero/one/two registered principals, one/two repositories,
and the fail-closed contract that abundant data never silently upgrades
an unregistered floor to ``pass``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE, CoordinatorRuntime
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
)

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=60)


def _stage1_request(
    *,
    run_id: str,
    repository: str,
    work_kind: WorkKind,
    risk_class: RiskClass,
    tmp_path: Path,
) -> Stage1RunRequest:
    manifest = issue_to_pr_manifest()
    work_item = WorkItem(
        id=WorkItemId(f"{run_id}-work-item"),
        work_kind=work_kind,
        source_provider="github",
        external_reference=f"{repository}#1",
        source_snapshot_reference=f"issue:1@{run_id}",
        title="Bounded change",
        objective="Change one bounded behavior.",
        acceptance_criteria=("observable result",),
        constraints=("retain evidence",),
        risk_class=risk_class,
        repository_targets=(repository,),
        dependencies=(),
        state=WorkItemState.QUALIFYING,
    )
    snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision=f"{run_id}-revision")
    root = tmp_path / run_id
    return Stage1RunRequest(
        run=Run(
            id=RunId(run_id),
            work_item_id=work_item.id,
            work_item_snapshot_digest=work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository=repository,
            base_revision="base-revision",
            policy_set_version="policy-v1",
            adapter_versions={},
            adapter_fingerprints={},
            capability_lock_digest=Digest.of_bytes(b"capability-lock"),
            environment_manifest_digest=Digest.of_bytes(b"environment"),
            configuration_snapshot_digest=Digest.of_bytes(b"configuration"),
            state=RunState.CREATED,
        ),
        work_snapshot=snapshot,
        manifest=manifest,
        repository_id=repository,
        repository_path=(root / "repository").resolve(),
        workspace_path=(root / "workspace").resolve(),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository=repository,
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            harness_provider="omp",
            harness_credential_reference="test-omp-keyring",
            harness_executable="omp",
            artifact_root=(root / "artifacts").resolve(),
        ),
        base_branch="main",
        head_branch=f"enginery/{run_id}",
        validation_commands=(("uv", "run", "pytest", "-q"),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id=f"implement-{run_id}",
            operation_id=OperationId(f"implement:{run_id}"),
            time_budget_seconds=60,
            cost_budget=Decimal("1.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
    )


def _register_completed_run(
    ledger: LedgerService,
    *,
    run_id: str,
    repository: str,
    work_kind: WorkKind,
    risk_class: RiskClass,
    tmp_path: Path,
) -> None:
    """Register a Stage 1 run and mark it completed via the same durable
    ``"{run_id}:verify"`` signal ``verify_merge_ready`` writes in
    production -- ``status: "passed"`` on the runtime node, not a
    ``Run.state`` mutation (see the gate CLI module docstring)."""
    request = _stage1_request(
        run_id=run_id,
        repository=repository,
        work_kind=work_kind,
        risk_class=risk_class,
        tmp_path=tmp_path,
    )
    CoordinatorRuntime(ledger, owner="coordinator").register_run(
        run_id=run_id, initial_state=request.initial_state(), now=_NOW, heartbeat_window=_HEARTBEAT
    )
    ledger.append(
        AppendCommand(
            correlation_id=f"gate-fixture-verify:{run_id}",
            events=(
                EventWrite(
                    aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
                    aggregate_id=f"{run_id}:verify",
                    expected_version=0,
                    event_type="runtime_node.queued",
                    schema_version=1,
                    payload={"run_id": run_id, "node_id": "verify", "status": "passed"},
                ),
            ),
        )
    )


def _record_intervention(
    ledger: LedgerService, *, run_id: str, node_id: str, decision: str, reason: str
) -> None:
    ledger.append(
        AppendCommand(
            correlation_id=f"gate-fixture-intervention:{run_id}:{node_id}",
            events=(
                EventWrite(
                    aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE,
                    aggregate_id=f"{run_id}:{node_id}",
                    expected_version=0,
                    event_type="runtime_node.human_wait_resolved",
                    schema_version=1,
                    payload={
                        "run_id": run_id,
                        "node_id": node_id,
                        "status": "passed",
                        "operator_decision": decision,
                        "reason": reason,
                    },
                ),
            ),
        )
    )


def _write_floor_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _gate_status(database: Path, floor_config: Path, capsys: pytest.CaptureFixture[str]) -> Any:
    main(
        [
            "gate",
            "status",
            "--gate",
            "G4",
            "--database",
            str(database),
            "--floor-config",
            str(floor_config),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    return payload


def _conditions(payload: Any) -> dict[str, Any]:
    return {condition["id"]: condition for condition in payload["conditions"]}


@pytest.mark.parametrize(
    ("principal_ids", "expected_status"),
    [
        ([], "fail"),
        (["operator-a"], "fail"),
        (["operator-a", "operator-b"], "pass"),
    ],
)
def test_registered_human_principals_zero_one_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    principal_ids: list[str],
    expected_status: str,
) -> None:
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()
    ids_toml = ", ".join(f'"{principal_id}"' for principal_id in principal_ids)
    floor_config = _write_floor_config(
        tmp_path / "floor.toml",
        f"schema_version = 1\n[registered_principals]\nids = [{ids_toml}]\n",
    )

    payload = _gate_status(database, floor_config, capsys)

    assert _conditions(payload)["registered_human_principals"]["status"] == expected_status
    assert _conditions(payload)["registered_human_principals"]["metrics"][
        "registered_principal_count"
    ] == len(set(principal_ids))


def test_corpus_diversity_one_repository_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _register_completed_run(
            ledger,
            run_id="run-1",
            repository="Mathews-Tom/only-repo",
            work_kind=WorkKind.ISSUE,
            risk_class=RiskClass.LOW,
            tmp_path=tmp_path,
        )
    finally:
        ledger.close()
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 1\n[registered_principals]\nids = []\n"
    )

    payload = _gate_status(database, floor_config, capsys)

    condition = _conditions(payload)["corpus_diversity"]
    assert condition["status"] == "fail"
    assert condition["metrics"]["repository_count"] == 1


def test_corpus_diversity_two_repositories_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        _register_completed_run(
            ledger,
            run_id="run-1",
            repository="Mathews-Tom/repo-one",
            work_kind=WorkKind.ISSUE,
            risk_class=RiskClass.LOW,
            tmp_path=tmp_path,
        )
        _register_completed_run(
            ledger,
            run_id="run-2",
            repository="Mathews-Tom/repo-two",
            work_kind=WorkKind.ISSUE,
            risk_class=RiskClass.LOW,
            tmp_path=tmp_path,
        )
    finally:
        ledger.close()
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 1\n[registered_principals]\nids = []\n"
    )

    payload = _gate_status(database, floor_config, capsys)

    condition = _conditions(payload)["corpus_diversity"]
    assert condition["status"] == "pass"
    assert condition["metrics"]["repository_count"] == 2


def test_floor_gated_conditions_report_unmeasured_despite_abundant_real_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prove the fail-closed contract: a large, genuinely diverse corpus
    of real ledger data never upgrades a condition to ``pass`` while its
    registered floor remains unset. Only a human editing the floor
    configuration file can change that -- this command reads, it never
    writes."""
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        for index, (work_kind, risk_class) in enumerate(
            [
                (WorkKind.ISSUE, RiskClass.LOW),
                (WorkKind.ISSUE, RiskClass.HIGH),
                (WorkKind.PLAN, RiskClass.LOW),
                (WorkKind.INCIDENT, RiskClass.MEDIUM),
            ]
        ):
            _register_completed_run(
                ledger,
                run_id=f"run-{index}",
                repository="Mathews-Tom/repo-one",
                work_kind=work_kind,
                risk_class=risk_class,
                tmp_path=tmp_path,
            )
            _record_intervention(
                ledger,
                run_id=f"run-{index}",
                node_id="plan_approval",
                decision="approved",
                reason=f"reviewed run-{index}",
            )
    finally:
        ledger.close()
    # No completed_runs / interventions / outcome_completeness floor is
    # registered -- only the (irrelevant to this assertion) principal
    # roster is present.
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 1\n[registered_principals]\nids = []\n"
    )

    payload = _gate_status(database, floor_config, capsys)

    conditions = _conditions(payload)
    assert conditions["completed_run_diversity"]["status"] == "unmeasured"
    assert conditions["completed_run_diversity"]["metrics"]["completed_run_count"] == 4
    assert conditions["completed_run_diversity"]["metrics"]["completed_workflow_type_count"] == 3
    assert conditions["completed_run_diversity"]["metrics"]["completed_risk_class_count"] == 3
    assert conditions["human_intervention_volume"]["status"] == "unmeasured"
    assert conditions["human_intervention_volume"]["metrics"]["intervention_with_reason_count"] == 4
    assert conditions["outcome_capture_completeness"]["status"] == "unmeasured"
    assert conditions["recurring_evidence_backed_deficiency"]["status"] == "unmeasured"
    assert payload["overall"] == "fail"


def test_floor_gated_conditions_pass_once_the_floor_is_registered_and_met(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    ledger = LedgerService.open(database)
    try:
        for index, (work_kind, risk_class) in enumerate(
            [
                (WorkKind.ISSUE, RiskClass.LOW),
                (WorkKind.PLAN, RiskClass.HIGH),
            ]
        ):
            _register_completed_run(
                ledger,
                run_id=f"run-{index}",
                repository="Mathews-Tom/repo-one",
                work_kind=work_kind,
                risk_class=risk_class,
                tmp_path=tmp_path,
            )
            _record_intervention(
                ledger,
                run_id=f"run-{index}",
                node_id="plan_approval",
                decision="approved",
                reason=f"reviewed run-{index}",
            )
    finally:
        ledger.close()
    floor_config = _write_floor_config(
        tmp_path / "floor.toml",
        """
        schema_version = 1
        [registered_principals]
        ids = []
        [completed_runs]
        min_total = 2
        [interventions]
        min_with_reason = 2
        [outcome_completeness]
        floor = 0.0
        """,
    )

    payload = _gate_status(database, floor_config, capsys)

    conditions = _conditions(payload)
    assert conditions["completed_run_diversity"]["status"] == "pass"
    assert conditions["human_intervention_volume"]["status"] == "pass"
    assert conditions["outcome_capture_completeness"]["status"] == "pass"
    # Still fail overall: corpus diversity (one repository), registered
    # principals (none), and the always-unmeasured recurring-deficiency
    # condition remain unsatisfied.
    assert payload["overall"] == "fail"


def test_registering_a_second_principal_by_hand_is_the_only_way_the_principal_condition_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command performs no registration action itself -- only
    editing the floor configuration file changes this condition, proving
    the "read-only, never mutates gate state" contract end to end."""
    database = tmp_path / "ledger.db"
    LedgerService.open(database).close()
    floor_config = tmp_path / "floor.toml"

    _write_floor_config(floor_config, "schema_version = 1\n[registered_principals]\nids = []\n")
    before = _gate_status(database, floor_config, capsys)
    assert _conditions(before)["registered_human_principals"]["status"] == "fail"

    _write_floor_config(
        floor_config,
        'schema_version = 1\n[registered_principals]\nids = ["operator-a", "operator-b"]\n',
    )
    after = _gate_status(database, floor_config, capsys)
    assert _conditions(after)["registered_human_principals"]["status"] == "pass"
