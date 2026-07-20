"""Composes Stage 2's merge -> prepare -> build -> publish -> verify workflow.

Each method is independently callable and re-callable: durable state
lives in the ``Stack``/``StackSlice`` ledger projection and the
fixture's own ``pyproject.toml``/``CHANGELOG.md``, never in this object,
so a coordinator restart resumes correctly by re-reading that state
rather than by replaying this object's in-memory fields. This mirrors
the "Plan to verified release" workflow's own acceptance language:
dependent milestones wait at hard barriers, version/changelog
preparation cannot begin before implementation gates pass, ambiguous
publication triggers reconciliation rather than republishing, and a
release is not marked complete until destination verification succeeds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enginery.adapters.github import GitHubReleaseAdapter, GitHubReleaseRequest
from enginery.adapters.pypi import PyPiAdapter
from enginery.application.delivery_ports import PublicationReceipt, PublicationRequest
from enginery.domain.enums import RiskClass
from enginery.domain.errors import (
    ExternalConflictError,
    HumanActionRequiredError,
    InvalidInputError,
    MissingPrerequisiteError,
    PolicyDenialError,
)
from enginery.domain.ids import OperationId, PlanMilestoneId, RunId, StackId
from enginery.domain.policy_decision import PolicyAction, PolicyResult
from enginery.domain.stack import Stack
from enginery.engine.fixture_build import BuiltFixtureArtifacts, FixtureBuilder
from enginery.engine.release_manifest import ReleaseManifest, VersionChangelogBroker
from enginery.policy.evaluator import PolicyEvaluator
from enginery.policy.schemas import ApprovalSchema
from enginery.workflows.merge_policy import MergePolicyService


@dataclass(frozen=True, slots=True)
class Stage2ReleaseWorkflow:
    """Owns Stage 2's five composed steps for one plan's stack and fixture release."""

    merge_policy: MergePolicyService
    release_manifest: VersionChangelogBroker
    fixture_builder: FixtureBuilder
    github_release: GitHubReleaseAdapter
    pypi: PyPiAdapter
    release_policy: PolicyEvaluator

    def merge_all(
        self,
        stack_id: StackId,
        *,
        pull_request_numbers: dict[PlanMilestoneId, int],
        required_checks: tuple[str, ...],
        require_approved_review: bool,
        risk_class: RiskClass,
        requesting_principal_id: str,
        now: datetime,
        heartbeat_window: timedelta,
    ) -> Stack:
        """Root-to-leaf merge every eligible slice, one policy-gated merge at a time.

        Stops and raises the moment a slice is not currently mergeable
        (stale evidence, denied policy, or an unresolved conflict) rather
        than silently skipping it or looping past a blocked slice --
        the caller reconciles (re-syncs, re-verifies CI) and retries the
        whole call once the blocking condition is resolved.
        """
        while True:
            stack = self.merge_policy.stacks.read(stack_id)
            if stack is None:
                raise MissingPrerequisiteError(
                    "stack does not exist", details={"stack_id": str(stack_id)}
                )
            milestone_id = stack.next_mergeable()
            if milestone_id is None:
                return stack
            pull_request_number = pull_request_numbers.get(milestone_id)
            if pull_request_number is None:
                raise InvalidInputError(
                    "no pull request number supplied for the next mergeable milestone",
                    details={"milestone_id": str(milestone_id)},
                )
            outcome = self.merge_policy.merge_next(
                stack_id,
                pull_request_number=pull_request_number,
                required_checks=required_checks,
                require_approved_review=require_approved_review,
                risk_class=risk_class,
                requesting_principal_id=requesting_principal_id,
                now=now,
                heartbeat_window=heartbeat_window,
            )
            if not outcome.merged:
                raise ExternalConflictError(
                    "merge did not complete for the next eligible slice",
                    details={"milestone_id": str(outcome.milestone_id), "detail": outcome.detail},
                )

    def prepare_release(
        self,
        manifest: ReleaseManifest,
        *,
        stack: Stack,
        risk_class: RiskClass,
        requesting_principal_id: str,
    ) -> ReleaseManifest:
        """Prepare version/changelog only after policy allows it and the stack is fully merged."""
        schema = ApprovalSchema(
            action=PolicyAction.RELEASE_PREPARE,
            risk_class=risk_class,
            target_resource=manifest.target.distribution_name,
            requesting_principal_id=requesting_principal_id,
        )
        decision = self.release_policy.evaluate(schema)
        if decision.result is not PolicyResult.ALLOW:
            raise PolicyDenialError(
                "policy does not permit release preparation",
                details={"policy_rule_id": decision.policy_rule_id},
            )
        return self.release_manifest.prepare(manifest, stack=stack)

    def build_and_verify_fixture(
        self, fixture_root: Path, *, expected_version: str, import_module: str
    ) -> BuiltFixtureArtifacts:
        """Build the fixture wheel/sdist and prove they install cleanly."""
        artifacts = self.fixture_builder.build(fixture_root, expected_version=expected_version)
        self.fixture_builder.verify_clean_install(
            artifacts, import_module=import_module, expected_version=expected_version
        )
        return artifacts

    def publish(
        self,
        artifacts: BuiltFixtureArtifacts,
        *,
        run_id: RunId,
        github_request: GitHubReleaseRequest,
        risk_class: RiskClass,
        requesting_principal_id: str,
    ) -> tuple[PublicationReceipt, PublicationReceipt]:
        """Publish to PyPI then GitHub Release, gated on a current human approval.

        ``release.publish`` is hard-required-human (``policy/rules.py``);
        without a recorded, current, digest-bound approval for this exact
        request, this raises rather than publishing -- there is no
        fallback path that skips human approval for a live destination.
        """
        schema = ApprovalSchema(
            action=PolicyAction.RELEASE_PUBLISH,
            risk_class=risk_class,
            target_resource=github_request.tag_name,
            diff_or_artifact_digest=str(artifacts.wheel.digest),
            requesting_principal_id=requesting_principal_id,
        )
        decision = self.release_policy.evaluate(schema)
        if decision.result is not PolicyResult.ALLOW:
            raise HumanActionRequiredError(
                "release.publish requires a current, interactive human approval "
                "before any live publish",
                details={
                    "policy_rule_id": decision.policy_rule_id,
                    "result": decision.result.value,
                },
            )
        self.pypi.stage(artifacts.wheel_path, artifacts.sdist_path)
        pypi_receipt = self.pypi.publish(
            PublicationRequest(
                run_id=run_id,
                artifact=artifacts.wheel,
                destination="pypi",
                operation_id=_publish_operation_id(run_id, "pypi", artifacts.wheel.version),
            )
        )
        self.github_release.stage(artifacts.wheel.digest, github_request)
        github_receipt = self.github_release.publish(
            PublicationRequest(
                run_id=run_id,
                artifact=artifacts.wheel,
                destination="github-release",
                operation_id=_publish_operation_id(
                    run_id, "github-release", artifacts.wheel.version
                ),
            )
        )
        return pypi_receipt, github_receipt

    def verify_destinations(
        self, pypi_receipt: PublicationReceipt, github_receipt: PublicationReceipt
    ) -> tuple[PublicationReceipt, PublicationReceipt]:
        """Confirm both destinations independently report the expected digest."""
        return self.pypi.verify(pypi_receipt), self.github_release.verify(github_receipt)


def _publish_operation_id(run_id: RunId, destination: str, version: str) -> OperationId:
    """A stable operation identity for one destination's publish, reused across retries."""
    payload = "\x1f".join(("stage2-publish", str(run_id), destination, version))
    return OperationId(value=hashlib.sha256(payload.encode("utf-8")).hexdigest())


__all__ = ["Stage2ReleaseWorkflow"]
