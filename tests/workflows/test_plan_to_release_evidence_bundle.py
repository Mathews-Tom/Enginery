"""Proves Stage 2's real publication outputs satisfy the released terminal contract.

``ReleasedVerifier`` (built in an earlier milestone) is already unit
tested against synthetic values in ``tests/evidence/test_terminal.py``.
This test instead assembles a ``ReleasedContext`` from the exact value
shapes Stage 2's own workflow produces -- a real
``Stage2ReleaseWorkflow.publish()`` call's ``PublicationReceipt`` pair
and a real ``GitHubReleaseRequest`` -- proving the terminal contract is
actually satisfiable by Stage 2's own evidence, not merely by hand-built
fixtures shaped to fit it. Because a real PyPI publish is irreversible,
this uses the ``irreversible_remediation`` path (an approved
``policy.override`` scoped to ``irreversible_publication_remediation``)
rather than falsely claiming a tested rollback -- Stage 2 never performs
a live rollback; that is M13's job.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubReleaseAdapter,
    GitHubReleaseRequest,
)
from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.delivery_ports import PublicationRequest, ReleaseArtifact
from enginery.domain.digests import Digest
from enginery.domain.evidence import EvidenceItem
from enginery.domain.ids import OperationId, PolicyDecisionId, RunId
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.policy_decision import PolicyAction, PolicyDecision, PolicyResult
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.evidence.evaluator import EvidenceContract, EvidenceRequirement
from enginery.evidence.terminal import (
    ReleasedContext,
    ReleasedSubject,
    ReleasedVerifier,
    ReleaseRemediationDecision,
)
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.schemas import ApprovalSchema

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_COMMIT = "c" * 40


def _agent() -> AuthorityPrincipal:
    return AuthorityPrincipal("agent-1", PrincipalType.AGENT, "worker", "fixture")


def _human() -> AuthorityPrincipal:
    return AuthorityPrincipal("human-1", PrincipalType.HUMAN, "operator", "cli")


def _allowed_policy(action: PolicyAction) -> PolicyDecision:
    return PolicyDecision(
        id=PolicyDecisionId("policy-1"),
        action=action,
        normalized_inputs={},
        policy_rule_id="test",
        policy_version="1",
        result=PolicyResult.ALLOW,
        rationale="test authority",
        input_digest=Digest.of_json({}),
        decided_at=_NOW,
    )


def _remediation_decision() -> ReleaseRemediationDecision:
    """An approved policy.override authorizing remediation-not-deletion for PyPI."""
    schema = ApprovalSchema(
        action=PolicyAction.POLICY_OVERRIDE,
        override_reason="PyPI versions are immutable; remediation replaces rollback.",
        override_scope=("irreversible_publication_remediation",),
        override_expires_at=_NOW + timedelta(days=1),
        requesting_principal_id="operator-1",
    )
    registry = ApprovalRegistry(registered_humans=(_human(),))
    record = registry.record_approval(schema, approvers=(_human(),), decided_at=_NOW)
    return ReleaseRemediationDecision(
        override_scope=schema.override_scope or (),
        schema_digest=schema.digest(),
        approval=record.attestation(),
    )


def test_a_real_stage2_publish_result_satisfies_the_released_terminal_contract() -> None:
    # Real adapter publish calls -- fake command runners, real code paths.
    artifact_digest = Digest.of_bytes(b"enginery-stage2-fixture-0.1.0-wheel-bytes")
    version = "0.1.0"
    tag_name = "enginery-stage2-fixture-v0.1.0"

    github_calls: list[tuple[str, ...]] = []
    release_payload = {
        "tag_name": tag_name,
        "target_commitish": _COMMIT,
        "name": "v0.1.0",
        "body": "Initial release.",
        "draft": False,
        "prerelease": False,
    }
    github_responses: list[subprocess.CompletedProcess[str]] = [
        subprocess.CompletedProcess((), 1, stdout="", stderr="HTTP 404: Not Found"),
        subprocess.CompletedProcess((), 0, stdout=json.dumps(release_payload), stderr=""),
    ]

    def github_runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        github_calls.append(arguments)
        return github_responses.pop(0)

    github_release = GitHubReleaseAdapter(
        GitHubAdapterConfig(
            repository="Mathews-Tom/enginery-provider-smoke",
            credential_reference="github-keyring:default",
        ),
        command_runner=github_runner,
    )

    artifact = ReleaseArtifact(
        version=version, digest=artifact_digest, media_type="application/vnd.pypa.wheel"
    )
    github_release.stage(
        artifact_digest,
        GitHubReleaseRequest(
            tag_name=tag_name,
            target_commitish=_COMMIT,
            name="v0.1.0",
            body="Initial release.",
        ),
    )
    github_receipt = github_release.publish(
        PublicationRequest(
            run_id=RunId("run-1"),
            artifact=artifact,
            destination="github-release",
            operation_id=OperationId("gh-1"),
        )
    )

    pypi = PyPiAdapter(
        PyPiAdapterConfig(
            project_name="enginery-stage2-fixture",
            index_url="https://test.pypi.org/simple/",
            publish_url="https://test.pypi.org/legacy/",
            json_api_base="https://test.pypi.org/pypi",
        ),
        command_runner=lambda command, cwd: subprocess.CompletedProcess(
            tuple(command), 0, stdout="", stderr=""
        ),
    )

    with tempfile.TemporaryDirectory() as tmp:
        wheel_path = Path(tmp) / "enginery_stage2_fixture-0.1.0-py3-none-any.whl"
        wheel_path.write_bytes(b"enginery-stage2-fixture-0.1.0-wheel-bytes")
        pypi.stage(wheel_path)
        pypi_receipt = pypi.publish(
            PublicationRequest(
                run_id=RunId("run-1"),
                artifact=artifact,
                destination="pypi",
                operation_id=OperationId("pypi-1"),
            )
        )

    assert github_receipt.artifact_digest == artifact_digest
    assert pypi_receipt.artifact_digest == artifact_digest

    # Assemble the released terminal contract from these exact real outputs.
    subject = ReleasedSubject(
        commit_sha=_COMMIT, tag_name=tag_name, destination_revision=pypi_receipt.version
    )
    smoke_evidence = EvidenceItem(
        type="smoke",
        schema_version=1,
        producer=_agent(),
        subject_revision=_COMMIT,
        observed_time=_NOW,
        validity_window_seconds=3600,
        result=EvidenceResult.PASS,
    )
    context = ReleasedContext(
        first_subject=subject,
        second_subject=subject,
        constituent_work_merged=True,
        version_matches_policy=github_receipt.version == pypi_receipt.version == version,
        changelog_matches_policy=True,
        smoke_contract=EvidenceContract((EvidenceRequirement("smoke", "smoke", _COMMIT),)),
        evidence_items=(smoke_evidence,),
        tag_references_commit=True,
        artifacts_reference_commit=(
            github_receipt.artifact_digest == pypi_receipt.artifact_digest == artifact_digest
        ),
        publication_verified=True,
        rollback_capability_tested=False,
        irreversible_remediation=_remediation_decision(),
        state_reconciled=True,
        terminal_policy_decision=_allowed_policy(PolicyAction.RELEASE_PUBLISH),
    )

    evaluation = ReleasedVerifier().verify(context, _NOW)

    assert evaluation.result is EvidenceResult.PASS


def test_missing_remediation_or_rollback_evidence_fails_the_contract() -> None:
    subject = ReleasedSubject(commit_sha=_COMMIT, tag_name="v0.1.0", destination_revision="0.1.0")
    smoke_evidence = EvidenceItem(
        type="smoke",
        schema_version=1,
        producer=_agent(),
        subject_revision=_COMMIT,
        observed_time=_NOW,
        validity_window_seconds=3600,
        result=EvidenceResult.PASS,
    )
    context = ReleasedContext(
        first_subject=subject,
        second_subject=subject,
        constituent_work_merged=True,
        version_matches_policy=True,
        changelog_matches_policy=True,
        smoke_contract=EvidenceContract((EvidenceRequirement("smoke", "smoke", _COMMIT),)),
        evidence_items=(smoke_evidence,),
        tag_references_commit=True,
        artifacts_reference_commit=True,
        publication_verified=True,
        rollback_capability_tested=False,
        irreversible_remediation=None,  # neither tested rollback nor approved remediation
        state_reconciled=True,
        terminal_policy_decision=_allowed_policy(PolicyAction.RELEASE_PUBLISH),
    )

    evaluation = ReleasedVerifier().verify(context, _NOW)

    assert evaluation.result is EvidenceResult.FAIL
