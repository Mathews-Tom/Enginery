from __future__ import annotations

import json
import os
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubPullRequests, GitHubWorkLedger
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.work_ports import HarnessTask, LifecycleProjection, PullRequestRequest
from enginery.domain.errors import TransientProviderFailureError
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.artifact_store import ArtifactStore

_REPOSITORY = "Mathews-Tom/enginery-provider-smoke"


def _enabled() -> bool:
    return os.environ.get("ENGINERY_PROVIDER_SMOKE") == "1"


def _run(*arguments: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


@pytest.mark.provider_smoke
def test_github_and_omp_provider_smoke(tmp_path: Path) -> None:
    if not _enabled():
        pytest.skip("not-run: set ENGINERY_PROVIDER_SMOKE=1 to authorize live provider mutation")

    config = GitHubAdapterConfig(
        repository=_REPOSITORY,
        credential_reference="github-keyring:default",
    )
    config.require_smoke_repository()
    suffix = uuid.uuid4().hex[:12]
    issue_title = f"enginery provider smoke {suffix}"
    issue = json.loads(
        _run(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{_REPOSITORY}/issues",
            "--raw-field",
            f"title={issue_title}",
            "--raw-field",
            "body=Provider smoke fixture.",
        )
    )
    issue_number = issue["number"]
    branch = f"enginery/smoke-{suffix}"
    checkout = tmp_path / "repository"
    pull_number: int | None = None
    cleanup_failures: list[str] = []
    try:
        _run("git", "clone", f"https://github.com/{_REPOSITORY}.git", str(checkout))
        _run("git", "config", "user.name", "Enginery Smoke", cwd=checkout)
        _run("git", "config", "user.email", "enginery-smoke@example.invalid", cwd=checkout)
        _run("git", "switch", "-c", branch, cwd=checkout)
        (checkout / "provider-smoke.txt").write_text(f"fixture {suffix}\n", encoding="utf-8")
        _run("git", "add", "provider-smoke.txt", cwd=checkout)
        _run("git", "commit", "-m", f"test: provider smoke {suffix}", cwd=checkout)
        _run("git", "push", "-u", "origin", branch, cwd=checkout)

        ledger = GitHubWorkLedger(config)
        snapshot = ledger.fetch(f"{_REPOSITORY}#{issue_number}")
        assert snapshot.work_item.external_reference == f"{_REPOSITORY}#{issue_number}"
        assert (
            ledger.publish_lifecycle(
                LifecycleProjection(
                    run_id=RunId(f"smoke-{suffix}"),
                    external_reference=f"{_REPOSITORY}#{issue_number}",
                    state="active",
                    evidence_digest=None,
                ),
                operation_id=OperationId(f"smoke-lifecycle-{suffix}"),
            )
            is ReconciliationResult.FOUND_MATCHING
        )

        operation_id = OperationId(f"smoke-pr-{suffix}")
        request = PullRequestRequest(
            head_branch=branch,
            base_branch="main",
            title=issue_title,
            body="Provider smoke fixture.",
            operation_id=operation_id,
        )

        def timeout_after_create(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
            completed = subprocess.run(arguments, check=False, capture_output=True, text=True)
            if (
                completed.returncode == 0
                and "POST" in arguments
                and f"repos/{_REPOSITORY}/pulls" in arguments
            ):
                return subprocess.CompletedProcess(
                    arguments, 1, stdout="", stderr="network timeout"
                )
            return completed

        uncertain = GitHubPullRequests(config, command_runner=timeout_after_create)
        with pytest.raises(TransientProviderFailureError):
            uncertain.create_or_update(request)

        pull_requests = GitHubPullRequests(config)
        assert (
            pull_requests.reconcile(operation_id=operation_id)
            is ReconciliationResult.FOUND_MATCHING
        )
        matching = pull_requests.create_or_update(request)
        pull_number = matching.number
        evidence = pull_requests.evidence(pull_number)
        assert evidence.pull_request.head_revision == matching.head_revision

        harness = OmpHarness(
            OmpAdapterConfig(credential_reference="omp-auth-profile:default"),
            ArtifactStore(tmp_path / "artifacts"),
        )
        session = harness.start(
            HarnessTask(
                run_id=RunId(f"smoke-{suffix}"),
                node_id=NodeId("omp-smoke"),
                attempt_id=NodeAttemptId("attempt-1"),
                operation_id=OperationId(f"smoke-omp-{suffix}"),
                workspace_path=checkout,
                objective="Do not modify files or run tools. Reply with exactly OMP_SMOKE_OK.",
                acceptance_criteria=("return OMP_SMOKE_OK",),
                constraints=("do not modify files",),
                permitted_capabilities=("repository-read",),
                evidence_requirements=("terminal JSON event",),
                time_budget_seconds=60,
                cost_budget=Decimal("1.00"),
            )
        )
        assert tuple(harness.events(session))
        assert harness.result(session).terminal_status == "succeeded"
    finally:
        if pull_number is not None:
            try:
                _run(
                    "gh", "pr", "close", str(pull_number), "--delete-branch", "--repo", _REPOSITORY
                )
            except subprocess.CalledProcessError:
                cleanup_failures.append(f"pull-request={pull_number}")
        else:
            try:
                _run("git", "push", "origin", "--delete", branch, cwd=checkout)
            except subprocess.CalledProcessError:
                cleanup_failures.append(f"branch={branch}")
        try:
            _run("gh", "issue", "close", str(issue_number), "--repo", _REPOSITORY)
        except subprocess.CalledProcessError:
            cleanup_failures.append(f"issue={issue_number}")
        if cleanup_failures:
            pytest.fail("retained provider smoke fixtures: " + ", ".join(cleanup_failures))
