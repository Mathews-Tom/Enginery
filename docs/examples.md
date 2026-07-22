# Example Workflows

Two complete, real Stage 1 request compositions: a fully local one you can
run right now with no external credentials, and a real GitHub/OMP-backed
one for when you are ready to run against a real repository.

## Example A: fully local, no live provider

This is the same fixture composition
`scripts/full_system_gate.py --stages 1 --restart-between-stages` uses to
prove cumulative Stage-1 recovery (see
[`docs/operations.md`](operations.md#cumulative-stage-1-recovery-evidence)).
It never contacts GitHub or spawns a real harness process — every port is
one of the deterministic local implementations `enginery adapter doctor`
reports. Run it directly to see a complete Stage 1 lifecycle, from
qualification through a registered outcome observation, execute end to
end in under a second:

```bash
uv run python scripts/full_system_gate.py --stages 1 --restart-between-stages
```

Read `_build_fixture` and `_drive_to_terminal` in that script for the
exact composition: a `WorkItem` and `Stage1RunRequest` built directly in
Python (not through any CLI command — there is no CLI path to a local
fixture provider in this release), a `Stage1RunService` wired to
in-script `WorkLedgerPort`/`PullRequestPort` fixtures, and the real
`review_implementation` → `open_pull_request` → `wait_for_ci` →
`verify_merge_ready` → `advance` (outcome registration) call sequence the
`enginery stage1` CLI commands use. This is the fastest way to see the
full lifecycle's shape without touching any external credential.

## Example B: real GitHub issue with an OMP or Claude Code harness

This is what actually running a real issue through Stage 1 looks like end
to end. It reuses the same request-building pattern
[`docs/migration-sage-dev.md`](migration-sage-dev.md#step-3-build-one-enginery-request-per-active-ticket)
uses for a migrated ticket, adapted for a fresh GitHub issue instead:

**Preferred:** `enginery stage1 build-request` composes this exact
document from flags — see
[`docs/operations.md`](operations.md#composing-a-request). The manual
Python composition below remains accurate and is kept for readers who
want to see the exact domain objects a request is built from, or who
need a field the command does not yet expose.

```python
# build_request.py
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.ids import RunId, WorkItemId, WorkflowDefinitionId
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
)

run_id = "issue-142"
manifest = issue_to_pr_manifest()

work_item = WorkItem(
    id=WorkItemId("work-issue-142"),
    work_kind=WorkKind.ISSUE,
    source_provider="github",
    external_reference="https://github.com/<owner>/<repo>/issues/142",
    source_snapshot_reference="issue:142@<observed-revision>",
    title="<issue title>",
    objective="<what a merge-ready PR must accomplish>",
    acceptance_criteria=("<criterion 1>", "<criterion 2>"),
    constraints=(),
    risk_class=RiskClass.LOW,
    repository_targets=("<owner>/<repo>",),
    dependencies=(),
    state=WorkItemState.QUALIFYING,
)
snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision="<observed-revision>")

run = Run(
    id=RunId(run_id),
    work_item_id=work_item.id,
    work_item_snapshot_digest=work_item.bound_field_digest,
    workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
    workflow_definition_digest=manifest.content_digest,
    repository="<owner>/<repo>",
    base_revision="<real base revision>",
    policy_set_version="policy-v1",
    adapter_versions={},
    adapter_fingerprints={},
    capability_lock_digest=Digest.of_bytes(b"no-capabilities-locked"),
    environment_manifest_digest=Digest.of_bytes(b"local-environment"),
    configuration_snapshot_digest=Digest.of_bytes(b"local-configuration"),
    state=RunState.CREATED,
)

request = Stage1RunRequest(
    run=run,
    work_snapshot=snapshot,
    manifest=manifest,
    repository_id="<owner>/<repo>",
    repository_path=Path("<absolute path to the local checkout>"),
    workspace_path=Path("<absolute path to a fresh workspace directory>"),
    base_branch="main",
    head_branch=f"enginery/{run_id}",
    validation_commands=(("uv", "run", "pytest", "-q"),),
    applicable_criteria=(True, True),
    required_checks=("CI",),
    repair_limit=1,
    implementation=Stage1ImplementationRequest(
        attempt_id="implement-0",
        operation_id=f"implement:{run_id}",
        time_budget_seconds=1800,
        cost_budget=Decimal("5.0"),
        permitted_capabilities=("git",),
        evidence_requirements=("redacted harness transcript",),
    ),
    execution_configuration=Stage1ExecutionConfiguration(
        github_repository="<owner>/<repo>",
        github_credential_reference="operator-gh-cli",
        github_executable="gh",
        harness_provider="omp",  # or "claude-code"
        harness_credential_reference="operator-harness-session",
        harness_executable="omp",
        artifact_root=Path("<absolute path to an artifact directory>"),
    ),
)

Path("request.json").write_text(json.dumps(request.initial_state(), indent=2), encoding="utf-8")
```

```bash
uv run enginery stage1 start --database ledger.db --owner operator --request request.json
uv run enginery stage1 watch --database ledger.db --owner operator --run-id issue-142 --advance
# repeat watch --advance until next_action is `await_human_review`
uv run enginery stage1 review --database ledger.db --owner operator --run-id issue-142 \
  --report review.json --repair-attempt 0
uv run enginery stage1 watch --database ledger.db --owner operator --run-id issue-142 --advance
# repeat until next_action is `wait` -- the run is merge_ready
uv run enginery stage1 evidence --database ledger.db --owner operator --run-id issue-142
```

Prerequisites: an authenticated `gh` CLI session with access to the target
repository, and either the `omp` or `claude` CLI installed and
authenticated for the chosen `harness_provider`. `enginery adapter doctor`
reports the local fixture providers only; there is no `enginery doctor`
check for GitHub/OMP/Claude Code availability in this release — probe them
directly (`gh auth status`, `omp --help`, `claude --version`) before
starting a real run. `stage1 review --report review.json` expects
`{"producer": "<agent or reviewer id>", "reviewer": "<reviewer id>",
"findings": [{"finding_id": "<id>", "actionable": false, "blocking": false}]}`
— an empty `findings` array means an approved review with no findings.
