"""Tests for enginery.workflows.plan_to_release."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubReleaseAdapter,
    GitHubReleaseRequest,
)
from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.adapter_types import AdapterStatus
from enginery.application.delivery_ports import ReleaseArtifact
from enginery.application.work_ports import (
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestRequest,
    PullRequestSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass
from enginery.domain.errors import (
    ExternalConflictError,
    HumanActionRequiredError,
    PolicyDenialError,
)
from enginery.domain.ids import OperationId, PlanId, PlanMilestoneId, RunId, StackId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.domain.stack import StackSliceState
from enginery.engine.fixture_build import FixtureBuilder
from enginery.engine.release_manifest import ReleaseManifest, ReleaseTarget, VersionChangelogBroker
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator, PolicyRule
from enginery.policy.schemas import ApprovalSchema
from enginery.workflows.merge_policy import MergePolicyService
from enginery.workflows.plan_to_release import Stage2ReleaseWorkflow

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_HEARTBEAT = timedelta(seconds=30)
_MILESTONES = (
    (PlanMilestoneId("m1"), "fixture/m1"),
    (PlanMilestoneId("m2"), "fixture/m2"),
)


class FakePullRequests:
    def __init__(self) -> None:
        self._evidence_by_number: dict[int, PullRequestEvidence] = {}
        self._merged: dict[int, PullRequestSnapshot] = {}
        self.merge_calls: list[int] = []

    def register(
        self, number: int, evidence: PullRequestEvidence, merged: PullRequestSnapshot
    ) -> None:
        self._evidence_by_number[number] = evidence
        self._merged[number] = merged

    def probe(self) -> AdapterStatus:  # pragma: no cover
        raise NotImplementedError

    def create_or_update(
        self, request: PullRequestRequest
    ) -> PullRequestSnapshot:  # pragma: no cover
        raise NotImplementedError

    def get(self, number: int) -> PullRequestSnapshot:  # pragma: no cover
        raise NotImplementedError

    def evidence(self, number: int) -> PullRequestEvidence:
        return self._evidence_by_number[number]

    def merge(
        self,
        number: int,
        *,
        expected_head_revision: str,
        operation_id: OperationId,
        merge_method: str = "merge",
    ) -> PullRequestSnapshot:
        self.merge_calls.append(number)
        return self._merged[number]

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        return ReconciliationResult.NOT_FOUND


def _stack_coordinator(ledger: LedgerService) -> StackCoordinator:
    return StackCoordinator(ledger, CoordinatorRuntime(ledger, owner="test-coordinator"))


def _merge_ready_two_slice_stack(coordinator: StackCoordinator, *, stack_id: StackId) -> None:
    coordinator.start(
        stack_id=stack_id,
        plan_id=PlanId("plan-1"),
        base_ref="main",
        ordered_milestones=_MILESTONES,
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    coordinator.reconcile_after_publish(
        stack_id,
        PlanMilestoneId("m1"),
        head_revision="m1-rev1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    coordinator.mark_merge_ready(
        stack_id,
        PlanMilestoneId("m1"),
        head_revision="m1-rev1",
        ci_evidence_digest=Digest.of_bytes(b"ci-m1"),
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    coordinator.reconcile_after_publish(
        stack_id,
        PlanMilestoneId("m2"),
        head_revision="m2-rev1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )
    coordinator.mark_merge_ready(
        stack_id,
        PlanMilestoneId("m2"),
        head_revision="m2-rev1",
        ci_evidence_digest=Digest.of_bytes(b"ci-m2"),
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )


def _pr_evidence(*, number: int, head: str, base: str) -> PullRequestEvidence:
    snapshot = PullRequestSnapshot(
        number=number,
        url=f"https://github.com/Mathews-Tom/enginery-provider-smoke/pull/{number}",
        state="open",
        head_branch=f"fixture/m{number}",
        head_revision=head,
        base_branch=base,
        base_revision="b" * 40,
    )
    return PullRequestEvidence(
        pull_request=snapshot,
        reviews=(),
        checks=(
            PullRequestCheck(
                name="ci", status="completed", conclusion="success", head_revision=head
            ),
        ),
        mergeable=True,
    )


def _pr_merged(*, number: int, head: str, base: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=number,
        url=f"https://github.com/Mathews-Tom/enginery-provider-smoke/pull/{number}",
        state="closed",
        head_branch=f"fixture/m{number}",
        head_revision=head,
        base_branch=base,
        base_revision="b" * 40,
        merged=True,
    )


def _allow_evaluator() -> PolicyEvaluator:
    return PolicyEvaluator(
        policy_version="1.0.0",
        rules=(
            PolicyRule(
                id="allow_merge",
                action=PolicyAction.PULL_REQUEST_MERGE,
                result=PolicyResult.ALLOW,
                rationale="test",
                risk_classes=frozenset({RiskClass.LOW}),
            ),
        ),
    )


def _fixture_root(tmp_path: Path, *, name: str = "enginery-stage2-fixture") -> Path:
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    return root


def _release_policy(
    *, rules: tuple[PolicyRule, ...] = (), registry: ApprovalRegistry | None = None
) -> PolicyEvaluator:
    return PolicyEvaluator(policy_version="1.0.0", rules=rules, approval_registry=registry)


class _NoOpCommandRunner:
    def __call__(self, command, cwd) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(tuple(command), 0, stdout="", stderr="")


def _default_github_runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="HTTP 404: Not Found")


def _workflow(
    tmp_path: Path,
    ledger: LedgerService,
    *,
    release_policy: PolicyEvaluator,
    github_command_runner=_default_github_runner,
) -> tuple[Stage2ReleaseWorkflow, FakePullRequests, StackCoordinator]:
    stacks = _stack_coordinator(ledger)
    pull_requests = FakePullRequests()
    merge_policy = MergePolicyService(
        stacks=stacks, pull_requests=pull_requests, policy=_allow_evaluator()
    )
    fixture_root = _fixture_root(tmp_path)
    workflow = Stage2ReleaseWorkflow(
        merge_policy=merge_policy,
        release_manifest=VersionChangelogBroker(fixture_root=fixture_root),
        fixture_builder=FixtureBuilder(command_runner=_NoOpCommandRunner()),
        github_release=GitHubReleaseAdapter(
            GitHubAdapterConfig(
                repository="Mathews-Tom/enginery-provider-smoke",
                credential_reference="github-keyring:default",
            ),
            command_runner=github_command_runner,
        ),
        pypi=PyPiAdapter(
            PyPiAdapterConfig(
                project_name="enginery-stage2-fixture",
                index_url="https://test.pypi.org/simple/",
                publish_url="https://test.pypi.org/legacy/",
                json_api_base="https://test.pypi.org/pypi",
            ),
            command_runner=lambda command, cwd: subprocess.CompletedProcess(
                tuple(command), 0, stdout="", stderr=""
            ),
        ),
        release_policy=release_policy,
    )
    return workflow, pull_requests, stacks


def test_merge_all_merges_every_slice_root_to_leaf(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    workflow, pull_requests, stacks = _workflow(
        tmp_path, ledger_service, release_policy=_release_policy()
    )
    stack_id = StackId("stack-1")
    _merge_ready_two_slice_stack(stacks, stack_id=stack_id)
    pull_requests.register(
        11,
        _pr_evidence(number=11, head="m1-rev1", base="main"),
        _pr_merged(number=11, head="m1-rev1", base="main"),
    )
    pull_requests.register(
        12,
        _pr_evidence(number=12, head="m2-rev1", base="fixture/m1"),
        _pr_merged(number=12, head="m2-rev1", base="fixture/m1"),
    )

    final_stack = workflow.merge_all(
        stack_id,
        pull_request_numbers={PlanMilestoneId("m1"): 11, PlanMilestoneId("m2"): 12},
        required_checks=("ci",),
        require_approved_review=False,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
        now=_NOW,
        heartbeat_window=_HEARTBEAT,
    )

    assert final_stack.next_mergeable() is None
    assert all(slice_.state is StackSliceState.MERGED for slice_ in final_stack.ordered_slices)
    assert pull_requests.merge_calls == [11, 12]


def test_merge_all_raises_when_a_slice_is_not_currently_mergeable(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    workflow, pull_requests, stacks = _workflow(
        tmp_path, ledger_service, release_policy=_release_policy()
    )
    stack_id = StackId("stack-1")
    _merge_ready_two_slice_stack(stacks, stack_id=stack_id)
    # Head diverged from what mark_merge_ready recorded -- evaluate_pull_request
    # will return SUPERSEDED.
    pull_requests.register(
        11,
        _pr_evidence(number=11, head="force-pushed", base="main"),
        _pr_merged(number=11, head="m1-rev1", base="main"),
    )

    with pytest.raises(ExternalConflictError):
        workflow.merge_all(
            stack_id,
            pull_request_numbers={PlanMilestoneId("m1"): 11},
            required_checks=("ci",),
            require_approved_review=False,
            risk_class=RiskClass.LOW,
            requesting_principal_id="operator-1",
            now=_NOW,
            heartbeat_window=_HEARTBEAT,
        )
    assert pull_requests.merge_calls == []


def test_prepare_release_denied_by_policy_raises(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    workflow, _, stacks = _workflow(
        tmp_path, ledger_service, release_policy=_release_policy()
    )  # no rules -> default deny
    stack_id = StackId("stack-1")
    _merge_ready_two_slice_stack(stacks, stack_id=stack_id)
    stack = stacks.mark_merged(
        stack_id, PlanMilestoneId("m1"), now=_NOW, heartbeat_window=_HEARTBEAT
    )
    stack = stacks.mark_merged(
        stack_id, PlanMilestoneId("m2"), now=_NOW, heartbeat_window=_HEARTBEAT
    )
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Initial release.",
    )

    with pytest.raises(PolicyDenialError):
        workflow.prepare_release(
            manifest, stack=stack, risk_class=RiskClass.LOW, requesting_principal_id="operator-1"
        )


def test_prepare_release_writes_through_broker_when_allowed(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    rules = (
        PolicyRule(
            id="allow_prepare",
            action=PolicyAction.RELEASE_PREPARE,
            result=PolicyResult.ALLOW,
            rationale="test",
            risk_classes=frozenset({RiskClass.LOW}),
        ),
    )
    workflow, _, stacks = _workflow(
        tmp_path, ledger_service, release_policy=_release_policy(rules=rules)
    )
    stack_id = StackId("stack-1")
    _merge_ready_two_slice_stack(stacks, stack_id=stack_id)
    stack = stacks.mark_merged(
        stack_id, PlanMilestoneId("m1"), now=_NOW, heartbeat_window=_HEARTBEAT
    )
    stack = stacks.mark_merged(
        stack_id, PlanMilestoneId("m2"), now=_NOW, heartbeat_window=_HEARTBEAT
    )
    manifest = ReleaseManifest(
        target=ReleaseTarget(distribution_name="enginery-stage2-fixture", version="0.1.0"),
        changelog_entry="Initial release.",
    )

    workflow.prepare_release(
        manifest, stack=stack, risk_class=RiskClass.LOW, requesting_principal_id="operator-1"
    )

    pyproject_text = (workflow.release_manifest.fixture_root / "pyproject.toml").read_text()
    assert 'version = "0.1.0"' in pyproject_text


def test_publish_raises_human_action_required_without_approval(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    from enginery.engine.fixture_build import BuiltFixtureArtifacts

    workflow, _, _ = _workflow(tmp_path, ledger_service, release_policy=_release_policy())
    artifacts = BuiltFixtureArtifacts(
        wheel=ReleaseArtifact(
            version="0.1.0",
            digest=Digest.of_bytes(b"wheel"),
            media_type="application/vnd.pypa.wheel",
        ),
        wheel_path=tmp_path / "w.whl",
        sdist=ReleaseArtifact(
            version="0.1.0", digest=Digest.of_bytes(b"sdist"), media_type="application/gzip"
        ),
        sdist_path=tmp_path / "s.tar.gz",
    )
    artifacts.wheel_path.write_bytes(b"wheel")
    artifacts.sdist_path.write_bytes(b"sdist")

    with pytest.raises(HumanActionRequiredError):
        workflow.publish(
            artifacts,
            run_id=RunId("run-1"),
            github_request=GitHubReleaseRequest(
                tag_name="enginery-stage2-fixture-v0.1.0",
                target_commitish="c" * 40,
                name="v0.1.0",
                body="Initial release.",
            ),
            risk_class=RiskClass.LOW,
            requesting_principal_id="operator-1",
        )


def test_publish_succeeds_after_a_recorded_human_approval(
    tmp_path: Path, ledger_service: LedgerService
) -> None:
    from enginery.engine.fixture_build import BuiltFixtureArtifacts

    human = AuthorityPrincipal(
        id="human-1",
        principal_type=PrincipalType.HUMAN,
        role="operator",
        authorization_source="cli",
    )
    registry = ApprovalRegistry(registered_humans=(human,))
    github_responses: list[subprocess.CompletedProcess[str]] = [
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found"),
        subprocess.CompletedProcess(
            (),
            0,
            stdout=(
                '{"tag_name": "enginery-stage2-fixture-v0.1.0", '
                f'"target_commitish": "{"c" * 40}", "name": "v0.1.0", '
                '"body": "Initial release.", "draft": false, "prerelease": false}'
            ),
            stderr="",
        ),
    ]

    def github_runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return github_responses.pop(0)

    workflow, _, _ = _workflow(
        tmp_path,
        ledger_service,
        release_policy=_release_policy(registry=registry),
        github_command_runner=github_runner,
    )
    artifacts = BuiltFixtureArtifacts(
        wheel=ReleaseArtifact(
            version="0.1.0",
            digest=Digest.of_bytes(b"wheel"),
            media_type="application/vnd.pypa.wheel",
        ),
        wheel_path=tmp_path / "w.whl",
        sdist=ReleaseArtifact(
            version="0.1.0", digest=Digest.of_bytes(b"sdist"), media_type="application/gzip"
        ),
        sdist_path=tmp_path / "s.tar.gz",
    )
    artifacts.wheel_path.write_bytes(b"wheel")
    artifacts.sdist_path.write_bytes(b"sdist")
    github_request = GitHubReleaseRequest(
        tag_name="enginery-stage2-fixture-v0.1.0",
        target_commitish="c" * 40,
        name="v0.1.0",
        body="Initial release.",
    )
    schema = ApprovalSchema(
        action=PolicyAction.RELEASE_PUBLISH,
        risk_class=RiskClass.LOW,
        target_resource=github_request.tag_name,
        diff_or_artifact_digest=str(artifacts.wheel.digest),
        requesting_principal_id="operator-1",
    )
    registry.record_approval(schema, approvers=(human,))

    pypi_receipt, github_receipt = workflow.publish(
        artifacts,
        run_id=RunId("run-1"),
        github_request=github_request,
        risk_class=RiskClass.LOW,
        requesting_principal_id="operator-1",
    )

    assert pypi_receipt.version == "0.1.0"
    assert github_receipt.version == "0.1.0"
