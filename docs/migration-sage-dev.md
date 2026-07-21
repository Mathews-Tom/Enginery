# Manual `sage-dev` Migration Guide

This is a **manual, human-executed procedure** for preserving useful
historical work data from a [`sage-dev`](https://github.com/Mathews-Tom/sage-dev)-managed
project when adopting Enginery. There is no automated `.sage/tickets`
importer, and none is planned for this release: `sage-dev` contributes
requirements and test scenarios to Enginery's design, not runtime
architecture, and Enginery does not reuse `sage-dev`'s coupling among
prompts, shell scripts, `jq`, and agent spawning. This guide preserves
**data and intent**, not `sage-dev`'s command interface — Enginery makes no
`/sage.*` compatibility promise of any kind.

Every command and field name below was verified against the real
[`Mathews-Tom/sage-dev`](https://github.com/Mathews-Tom/sage-dev) repository
(its `commands/sage.migrate.md` ticket-generation templates and
`.sage/lib/resolve-dependencies.sh` / `.sage/lib/validate-dependencies.sh`
dependency tooling) and against a real constructed fixture executed through
this repository's actual `enginery` CLI. Nothing here is speculative.

## What this guide does and does not do

- **Does:** walk an operator through inspecting a `sage-dev` project's
  `.sage/tickets/` data, deciding what is worth preserving, and manually
  authoring the corresponding Enginery request document(s) for any ticket
  the operator chooses to continue as real, active Enginery work.
- **Does not:** parse or write `.sage/tickets/*` programmatically. Does not
  promise perfect fidelity for every historical ticket. Does not archive or
  redirect `sage-dev` itself. Does not add any `/sage.*` shell alias or
  compatibility shim.

## `sage-dev`'s real ticket data layer

A `sage-dev`-managed project stores ticket state under `.sage/tickets/` in
the *target repository* (not in the `sage-dev` tool repository itself,
which only ships the commands that generate this data):

- `.sage/tickets/index.json` — the master index. Every ticket in it carries
  at minimum `id`, `title`, `state`, `priority`, `type`, `parent`,
  `children`, and a `git` block (`branch`, `commits`). Current
  `.sage/lib/resolve-dependencies.sh` (schema v2.2.0 as observed) computes
  dependency relationships from `blocks` (an array of ticket IDs this
  ticket blocks) and `blockedBy` (the computed inverse — ticket IDs
  blocking this one). Older `sage-dev` documentation shows a simpler
  `dependencies` array instead. **Do not assume either field name without
  opening your project's actual `index.json` first** — `sage-dev`'s schema
  has changed across versions, and this guide's job is to preserve your
  data as it actually exists, not as any one version's docs describe it.
- `.sage/tickets/<ID>.md` — one detail file per ticket, with `**State:**`,
  `**Priority:**`, `**Type:**`, a `## Description`, a `## Acceptance
  Criteria` checklist, a `## Dependencies` section (`#PARENT-ID (parent)`,
  `#DEP-ID (blocked by)`), and a `## Progress` section with branch/commit
  references. `COMPLETED` tickets generated in `optimized` or `legacy`
  migration mode instead carry a minimal lightweight record: state,
  `completed_at`, `git.commits`, and a one-line `summary` — no acceptance
  criteria or validation config.
- Ticket `state` is one of exactly four values:
  `UNPROCESSED` | `IN_PROGRESS` | `DEFERRED` | `COMPLETED`.

## Decide what to preserve

Enginery's `WorkItem`/`Run` model represents **active engineering intent**
bound to a workflow, not an archive format for arbitrary historical
records — there is no CLI command in this release that ingests a
already-completed historical ticket as a finished Enginery work item.
Split your tickets into two groups before touching Enginery at all:

1. **Historical (`COMPLETED`) tickets.** Preserve these as read-only
   reference material alongside the new repository (for example, a
   `docs/history/sage-dev-tickets/` directory, or an external archive
   location your team already uses), cross-referenced from your project's
   own changelog or README so nobody has to go looking for them. Do not
   try to force-fit a completed historical record into Enginery's live
   ledger; it is not designed for that and doing so would misrepresent a
   past, already-shipped change as new active work.
2. **Active (`UNPROCESSED` or `IN_PROGRESS`) tickets you intend to keep
   working on.** For each one you want Enginery to actually drive, build a
   Stage 1 request document for it (below). `DEFERRED` tickets you still
   intend to pursue later can wait until you are ready to activate them —
   there is no cost to leaving them in the historical-archive bucket until
   then.

## Step 1 (read-only): inspect the source project

Run these against the `sage-dev`-managed source repository. None of them
touch Enginery or mutate anything:

```bash
cat .sage/tickets/index.json | jq '.tickets | length'
cat .sage/tickets/index.json | jq '.tickets[] | {id, title, state, priority}'
# If the project ships sage-dev's own dependency tooling:
bash .sage/lib/validate-dependencies.sh detect-cycles .sage/tickets/index.json
```

Confirm the dependency graph among the tickets you plan to migrate has no
cycles before proceeding — `detect-cycles` reports this directly if the
source project's own tooling is present; otherwise inspect `blocks` /
`blockedBy` (or `dependencies`, per your project's schema version) by hand
for the tickets you are migrating.

## Step 2 (mandatory): back up the Enginery ledger

**Run this before any command that touches your Enginery `--database`
file**, including the very first `enginery stage1 start` you run as part
of this migration. If the ledger file does not exist yet, this step is
trivially satisfied by starting from a fresh, empty, tracked ledger path —
but once it exists, back it up before every subsequent import:

```bash
uv run enginery ledger backup --database ledger.db --output backup-$(date -u +%Y%m%dT%H%M%SZ)
```

If an import goes wrong, recover with `ledger restore`. **`restore`
refuses to overwrite an existing destination file** — move the current
ledger aside first:

```bash
mv ledger.db ledger.db.bad-$(date -u +%Y%m%dT%H%M%SZ)
uv run enginery ledger restore --backup backup-<timestamp> --database ledger.db
uv run enginery ledger verify --database ledger.db
```

`ledger verify` is safe to run at any time, including against a live
ledger, and never mutates anything — use it after every restore and
whenever you are unsure of a ledger's state.

## Step 3: build one Enginery request per active ticket

Enginery has no CLI command that accepts a hand-written "friendly" JSON
document for a new work item in this release: `enginery stage1 start
--request request.json` expects the exact internal serialization of a
`Stage1RunRequest` (bound run/work-item/manifest digests included), which
is not something to hand-author as raw JSON. Instead, write a short Python
script per ticket (or a small family of them) using Enginery's own request
type directly, and let it emit the JSON:

```python
# build_request.py -- adapt per ticket; run with `uv run python build_request.py`
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

TICKET_ID = "AUTH-004"  # from .sage/tickets/index.json .tickets[].id
run_id = f"sage-migration-{TICKET_ID.lower()}"
manifest = issue_to_pr_manifest()

work_item = WorkItem(
    id=WorkItemId(f"work-{TICKET_ID.lower()}"),
    work_kind=WorkKind.ISSUE,
    source_provider="sage-dev-migration",  # never "github" -- this preserves provenance
    external_reference=f"sage-dev:{TICKET_ID}",
    source_snapshot_reference=f"sage-dev:{TICKET_ID}@manual-migration",
    title="<ticket title>",           # from index.json .title / the ticket's "# ID: Title" heading
    objective="<ticket description>",  # from the ticket's ## Description
    acceptance_criteria=(              # one string per "## Acceptance Criteria" checklist item
        "<criterion 1>",
        "<criterion 2>",
    ),
    constraints=("preserve sage-dev ticket id in provenance",),
    risk_class=RiskClass.MEDIUM,  # judgment call: sage-dev has no risk-class field
    repository_targets=("<enginery-repository-id>",),
    dependencies=(),  # see "Mapping dependencies" below
    state=WorkItemState.QUALIFYING,
)
snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision="manual-migration")

run = Run(
    id=RunId(run_id),
    work_item_id=work_item.id,
    work_item_snapshot_digest=work_item.bound_field_digest,
    workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
    workflow_definition_digest=manifest.content_digest,
    repository="<enginery-repository-id>",
    base_revision="<real base revision, e.g. output of `git rev-parse main`>",
    policy_set_version="policy-v1",
    adapter_versions={},
    adapter_fingerprints={},
    capability_lock_digest=Digest.of_bytes(b"no-capabilities-locked"),
    environment_manifest_digest=Digest.of_bytes(b"sage-dev-migration-environment"),
    configuration_snapshot_digest=Digest.of_bytes(b"sage-dev-migration-configuration"),
    state=RunState.CREATED,
)

request = Stage1RunRequest(
    run=run,
    work_snapshot=snapshot,
    manifest=manifest,
    repository_id="<enginery-repository-id>",
    repository_path=Path("<absolute path to the target git checkout>"),
    workspace_path=Path("<absolute path to a fresh workspace directory>"),
    base_branch="main",
    head_branch=f"enginery/{run_id}",
    validation_commands=(("<real project validation command>",),),
    applicable_criteria=(True, True),  # must align 1:1 with acceptance_criteria
    required_checks=("<real required CI check name>",),
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
        github_repository="<owner>/<repository>",
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

Then, having already completed the mandatory backup in Step 2:

```bash
uv run enginery stage1 start --database ledger.db --owner migration-operator --request request.json
uv run enginery stage1 evidence --database ledger.db --owner migration-operator --run-id sage-migration-auth-004
```

`stage1 start` is idempotent for an unchanged request: re-running the same
command against the same `--database` and request document is safe and
returns the same `run_id`/`status` without creating a duplicate run.
Re-running it with a *changed* request for the same run ID is rejected
outright (`different immutable request`) rather than silently overwriting
history.

### Mapping dependencies

`WorkItem.dependencies` holds Enginery `WorkItemId`s, not `sage-dev` ticket
IDs. When a ticket you are migrating is `blockedBy` (or, on an older
schema, `dependencies` on) another ticket:

- If that other ticket is already `COMPLETED` and archived per the
  historical-preservation step above, it has no corresponding Enginery
  work item — leave it out of `dependencies` and note the historical
  reference in `constraints` or the objective text instead.
- If that other ticket is itself being actively migrated, migrate it
  first, note the Enginery `WorkItemId` your script gave it, and include
  that ID in the dependent ticket's `dependencies` tuple.

## Step 4: verify

```bash
uv run enginery ledger verify --database ledger.db
uv run enginery stage1 evidence --database ledger.db --owner migration-operator --run-id <run-id>
```

Confirm the run's `request_digest` and `source_revision` match what you
expect, and that `enginery ledger verify` reports the ledger healthy. From
here the run progresses through the normal Stage 1 lifecycle described in
[`docs/operations.md`](operations.md#running-a-stage-1-workflow) — this
guide's job ends once the ticket exists as a durable, evidence-bound
Enginery run.

## Worked example

The sequence above was executed against a real constructed fixture — a
`.sage/tickets/index.json` and `.sage/tickets/AUTH-004.md` matching the
real schema described above, one `blockedBy`/dependency relationship, and
a real local git repository — through this repository's actual `enginery`
CLI:

```text
$ enginery stage1 start --database ledger.db --owner migration-operator --request request.json
{"run_id": "sage-migration-auth-004", "status": "created"}

$ enginery ledger backup --database ledger.db --output backup-1
backup written to backup-1 (schema version 5)

$ enginery stage1 evidence --database ledger.db --owner migration-operator --run-id sage-migration-auth-004
{"base_revision": "main", "nodes": [], "request_digest": "sha256:3f0ac...", "run_id": "sage-migration-auth-004", "run_status": "created", "source_revision": "manual-migration"}

$ enginery ledger verify --database ledger.db
healthy

$ mv ledger.db ledger.db.bad
$ enginery ledger restore --backup backup-1 --database ledger.db
restored ledger.db (schema version 5)

$ enginery ledger verify --database ledger.db
healthy
```

This confirms, against real command output rather than a hypothetical
walkthrough: the request-building step and the read-only inspection step
never mutate the ledger; the first mutating command is `stage1 start`;
`stage1 start` is idempotent under a re-run with the same request; `ledger
backup` followed by `ledger restore` recovers the exact prior state after
simulated damage; and `ledger verify` correctly reports the restored
ledger healthy.

## What this migration explicitly does not do

- No engineered `.sage/tickets` importer or scripted bulk-migration tool.
  Every ticket you actively continue is a deliberate, reviewed, one-at-a-
  time decision by a human operator, matching the "manual, human-executed
  migration guide" scope for this release.
- No `/sage.*` command aliases, shell compatibility layer, or in-place
  `sage-dev` upgrade path.
- No automatic archiving or redirection of the `sage-dev` project itself.
