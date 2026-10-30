"""Persisted state written before authority separation still decodes and keeps its identity.

The goldens below were computed by the code that wrote this state (the commit before environment
allowlists, operator publication and verified-head validation existed) for the fixed values in
these tests. Every new persisted field is omitted when it holds its default, so a run persisted
earlier decodes to the legacy contract and re-encodes to exactly the state it was stored as.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.application.worker_environment import LAUNCHER_KEYS, TRUSTED_LOCAL_WORKER_KEYS
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InternalInvariantViolationError, InvalidInputError
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.runner import LEGACY_RUNNER_SELECTION
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import (
    BRANCH_PUBLICATION_OPERATOR,
    BRANCH_PUBLICATION_WORKER,
    FixtureDispatch,
    _request_from_state,
    _request_state,
)
from enginery.engine.workspace import (
    WORKSPACE_MODE_GIT_WORKTREE,
    WORKSPACE_MODE_TASK_LOCAL_CLONE,
    _reservation_from_state,
    _reservation_state,
)
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.run_intent import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    stage1_request_from_state,
)

GOLDEN_REQUEST_DIGEST = "sha256:2205072a790ac24079edfc450403a9e8baec0dabe6f06629738a38615bb3d8ea"
GOLDEN_REQUEST_STATE_KEYS = [
    "applicable_criteria",
    "base_branch",
    "execution_configuration",
    "head_branch",
    "implementation",
    "manifest",
    "repair_limit",
    "repository_id",
    "repository_path",
    "request_digest",
    "required_checks",
    "run",
    "run_id",
    "source_revision",
    "status",
    "validation_commands",
    "work_item",
    "workspace_path",
]
GOLDEN_NODE_STATE = {
    "actor_type": "agent",
    "attempt_id": "implement-0",
    "base_revision": "base-revision-golden",
    "command": ["omp", "--print"],
    "dependencies": [["run-legacy", "qualify"]],
    "expected_attempt_version": 0,
    "log_directory": "/golden/artifacts/worker-logs",
    "node_id": "implement",
    "operation_id": "implement:run-legacy",
    "repository_id": "repository-1",
    "repository_path": "/golden/repository",
    "retain_workspace": True,
    "run_id": "run-legacy",
    "status": "queued",
    "workflow_definition_id": "issue-to-pr-v1",
    "workspace_path": "/golden/workspaces/run",
}
GOLDEN_RESERVATION_STATE = {
    "base_revision": "base-revision-golden",
    "repository_id": "repository-1",
    "repository_path": "/golden/repository",
    "run_id": "run-legacy",
    "status": "materialized",
    "workspace_path": "/golden/workspaces/run",
}


def _legacy_request() -> Stage1RunRequest:
    snapshot = WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("work-1"),
            work_kind=WorkKind.ISSUE,
            source_provider="github",
            external_reference="issue:1",
            source_snapshot_reference="issue:1@1",
            title="Bounded change",
            objective="Change one bounded behavior.",
            acceptance_criteria=("observable result",),
            constraints=("retain evidence",),
            risk_class=RiskClass.LOW,
            repository_targets=("repository-1",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="1",
    )
    manifest = stage1_work_manifest()
    return Stage1RunRequest(
        run=Run(
            id=RunId("run-legacy"),
            work_item_id=snapshot.work_item.id,
            work_item_snapshot_digest=snapshot.work_item.bound_field_digest,
            workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
            workflow_definition_digest=manifest.content_digest,
            repository="repository-1",
            base_revision="base-revision-golden",
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
        repository_id="repository-1",
        repository_path=Path("/golden/repository"),
        workspace_path=Path("/golden/workspaces/run"),
        base_branch="main",
        head_branch="enginery/run-legacy",
        validation_commands=(("uv", "run", "pytest", "-q"),),
        applicable_criteria=(True,),
        required_checks=("CI",),
        repair_limit=1,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:run-legacy"),
            time_budget_seconds=60,
            cost_budget=Decimal("5.0"),
            permitted_capabilities=("git",),
            evidence_requirements=("redacted harness transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/Enginery",
            github_credential_reference="test-github-keyring",
            github_executable="gh",
            harness_provider="omp",
            harness_credential_reference="test-omp-keyring",
            harness_executable="omp",
            artifact_root=Path("/golden/artifacts"),
        ),
        execution_selection=ExecutionSelection.legacy_host_default("omp"),
        runner=LEGACY_RUNNER_SELECTION,
    )


def _legacy_dispatch(**overrides: object) -> FixtureDispatch:
    fields: dict[str, object] = {
        "run_id": "run-legacy",
        "node_id": "implement",
        "attempt_id": "implement-0",
        "repository_id": "repository-1",
        "repository_path": Path("/golden/repository"),
        "workspace_path": Path("/golden/workspaces/run"),
        "base_revision": "base-revision-golden",
        "command": ("omp", "--print"),
        "expected_attempt_version": 0,
        "operation_id": "implement:run-legacy",
        "dependencies": (("run-legacy", "qualify"),),
        "workflow_definition_id": "issue-to-pr-v1",
        "retain_workspace": True,
        "log_directory": Path("/golden/artifacts/worker-logs"),
    }
    fields.update(overrides)
    return FixtureDispatch(**fields)  # type: ignore[arg-type]


# --- the persisted request ---------------------------------------------------------------------


def test_a_legacy_request_keeps_its_digest_state_and_carried_budget() -> None:
    request = _legacy_request()

    assert str(request.digest) == GOLDEN_REQUEST_DIGEST
    state = request.initial_state()
    assert sorted(state) == GOLDEN_REQUEST_STATE_KEYS
    implementation = state["implementation"]
    assert isinstance(implementation, dict) and implementation["cost_budget"] == "5.0"
    assert state["request_digest"] == GOLDEN_REQUEST_DIGEST


def test_a_legacy_request_decodes_from_its_stored_json_to_the_same_request_and_digest() -> None:
    stored = json.loads(json.dumps(_legacy_request().initial_state()))

    decoded = stage1_request_from_state(stored)

    assert decoded == _legacy_request()
    assert decoded.runner.legacy
    assert str(decoded.digest) == GOLDEN_REQUEST_DIGEST
    assert decoded.implementation.cost_budget == Decimal("5.0")
    assert decoded.initial_state() == stored


# --- the persisted runtime node ---------------------------------------------------------


def test_a_legacy_node_encodes_to_exactly_the_state_it_was_stored_as() -> None:
    dispatch = _legacy_dispatch()

    assert dispatch.environment_keys is None
    assert dispatch.branch_publication == BRANCH_PUBLICATION_WORKER
    assert _request_state(dispatch, status="queued", actor_type=ActorType.AGENT) == (
        GOLDEN_NODE_STATE
    )


def test_a_legacy_node_decodes_to_the_legacy_launch_and_publication_contract() -> None:
    decoded = _request_from_state(dict(GOLDEN_NODE_STATE))

    assert decoded == _legacy_dispatch()
    assert decoded.environment_keys is None, "the ambient-inheriting launch"
    assert decoded.branch_publication == BRANCH_PUBLICATION_WORKER, "the worker pushes"
    assert decoded.workspace_mode == WORKSPACE_MODE_GIT_WORKTREE
    assert _request_state(decoded, status="queued", actor_type=ActorType.AGENT) == GOLDEN_NODE_STATE


# --- the persisted reservation ----------------------------------------------------------


def test_a_legacy_reservation_decodes_and_re_encodes_to_the_same_state() -> None:
    decoded = _reservation_from_state(dict(GOLDEN_RESERVATION_STATE), 3)

    assert decoded.mode == WORKSPACE_MODE_GIT_WORKTREE
    assert decoded.status == "materialized"
    assert decoded.state_version == 3
    assert (
        _reservation_state(
            decoded.repository_id,
            decoded.run_id,
            decoded.repository_path,
            decoded.workspace_path,
            decoded.base_revision,
            decoded.status,
            decoded.mode,
        )
        == GOLDEN_RESERVATION_STATE
    )
    assert "mode" not in GOLDEN_RESERVATION_STATE
    assert (
        _reservation_state(
            "repository-1",
            "run-legacy",
            Path("/golden/repository"),
            Path("/golden/workspaces/run"),
            "base-revision-golden",
            "materialized",
            WORKSPACE_MODE_TASK_LOCAL_CLONE,
        )["mode"]
        == WORKSPACE_MODE_TASK_LOCAL_CLONE
    )


# --- the new keys -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "new_keys"),
    [
        ({}, set()),
        ({"environment_keys": TRUSTED_LOCAL_WORKER_KEYS}, {"environment_keys"}),
        ({"branch_publication": BRANCH_PUBLICATION_OPERATOR}, {"branch_publication"}),
        (
            {
                "environment_keys": LAUNCHER_KEYS,
                "branch_publication": BRANCH_PUBLICATION_OPERATOR,
                "workspace_mode": WORKSPACE_MODE_TASK_LOCAL_CLONE,
            },
            {"environment_keys", "branch_publication", "workspace_mode"},
        ),
    ],
    ids=["legacy", "environment-only", "publication-only", "new-isolated-run"],
)
def test_each_new_persisted_key_appears_only_when_it_is_not_its_default(
    overrides: dict[str, object], new_keys: set[str]
) -> None:
    dispatch = _legacy_dispatch(**overrides)

    state = _request_state(dispatch, status="queued", actor_type=ActorType.AGENT)

    assert set(state) - set(GOLDEN_NODE_STATE) == new_keys
    if "environment_keys" in new_keys:
        assert state["environment_keys"] == list(dispatch.environment_keys or ())
    if "branch_publication" in new_keys:
        assert state["branch_publication"] == BRANCH_PUBLICATION_OPERATOR
    round_tripped = _request_from_state(json.loads(json.dumps(state)))
    assert round_tripped == dispatch


def test_a_new_node_round_trips_through_its_stored_json() -> None:
    dispatch = _legacy_dispatch(
        environment_keys=TRUSTED_LOCAL_WORKER_KEYS, branch_publication=BRANCH_PUBLICATION_OPERATOR
    )
    stored = json.loads(
        json.dumps(_request_state(dispatch, status="queued", actor_type=ActorType.AGENT))
    )

    decoded = _request_from_state(stored)

    assert decoded.environment_keys == TRUSTED_LOCAL_WORKER_KEYS
    assert decoded.branch_publication == BRANCH_PUBLICATION_OPERATOR


# --- refusing what must not be stored or launched ---------------------------------------


@pytest.mark.parametrize("key", ["GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "UV_PUBLISH_TOKEN"])
def test_a_dispatch_can_never_permit_a_credential_shaped_environment_key(key: str) -> None:
    with pytest.raises(InvalidInputError, match="credential-shaped"):
        _legacy_dispatch(environment_keys=("PATH", key))


def test_a_dispatch_refuses_an_unknown_publication_contract() -> None:
    with pytest.raises(InvalidInputError, match="branch publication is not supported"):
        _legacy_dispatch(branch_publication="somebody-else")


@pytest.mark.parametrize(
    "corruption",
    [
        {"environment_keys": "PATH"},
        {"environment_keys": ["PATH", 7]},
        {"branch_publication": "somebody-else"},
        {"branch_publication": 3},
    ],
)
def test_corrupt_stored_authority_fields_are_refused_loudly_not_defaulted(
    corruption: dict[str, object],
) -> None:
    stored = {**GOLDEN_NODE_STATE, **corruption}

    with pytest.raises(InternalInvariantViolationError):
        _request_from_state(stored)


def test_a_stored_credential_shaped_environment_key_is_refused_on_decode() -> None:
    stored = {**GOLDEN_NODE_STATE, "environment_keys": ["PATH", "GITHUB_TOKEN"]}

    with pytest.raises(InvalidInputError, match="credential-shaped"):
        _request_from_state(stored)
