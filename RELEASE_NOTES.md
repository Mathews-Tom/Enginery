# Enginery `v0.1.0` Release Notes

**This is a Stage-1-only release.** Enginery coordinates a durable,
coordinator-owned issue-to-merge-ready-pull-request workflow against a
real GitHub repository and a real coding-agent harness, and captures raw,
versioned outcome observations for every run. Stage 2 (plan to verified
release) and Stage 3 (incident to hotfix and rollback) are separate,
later release trains — `v0.2.0` and `v0.3.0` — with their own milestones
and gates. Stage 4 (governed factory self-improvement) is additionally
**gate-deferred**: its milestones may not start until a data-threshold
entry gate passes (sufficient completed-run and intervention volume
across at least two workflow types and risk classes, an outcome-capture
completeness floor, at least one recurring evidence-backed workflow
deficiency, corpus diversity beyond a single repository, and a second
registered human principal). That gate has **no committed date** and is
evaluated on a review cadence, never by elapsed time.

## What ships in `v0.1.0`

- A durable event ledger (SQLite), transactional inbox/outbox,
  projections, backup/restore, and fail-closed schema migration.
- A named-action policy engine with default-deny evaluation and
  independently adversarial-tested hard rules.
- A coordinator-owned runtime: fenced leases, exclusive git-worktree
  workspaces, process-group supervision, and crash/restart recovery that
  never duplicates an external side effect.
- The complete Stage 1 lifecycle — qualify, implement, validate, review,
  bounded repair, open a pull request, wait for exact-head CI, verify
  merge-ready evidence, cancel/resume — proven against a real GitHub
  repository and a real coding-agent harness, including surviving a
  coordinator restart without a duplicate side effect.
- A versioned raw outcome-observation schema and Stage 1 adapters, with a
  fail-closed completeness metric that cannot be inflated by suppressing
  or delaying attribution.
- Operator, adapter, and migration documentation.

See [`CHANGELOG.md`](CHANGELOG.md) for the itemized list.

## Compatibility and known limitations

- `v0.1.0` publishes schema and API versions but makes **no `1.0`
  stability promise**. Runs bind adapter/version fingerprints and block a
  silent resume under different adapter behavior.
- Self-improvement (candidate evaluation, canary rollout, and promotion)
  does not exist in this release.
- There is no hosted or multi-tenant service, no organization RBAC, no
  browser dashboard, and no interactive TUI.
- **Windows is not supported.** Process supervision, cancellation, and
  recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace **separation**, not
  hostile-code containment: it does not prevent a process from reaching
  the operator's account, filesystem, network, or keychain.
- Medium- and high-risk work requires human final review; there is no
  autonomous unattended-merge mode.
- The repository already contains early, forward-looking groundwork for
  later release trains (a second coding-agent harness adapter,
  capability locking and provenance, a read-only Stage 2 stack-inspection
  command, and plan-to-child-run scheduling). None of it is a supported
  or documented feature of `v0.1.0`.

## Migrating from `sage-dev`

There is no automated `.sage/tickets` importer, and none is planned.
Preserving historical `sage-dev` work data is a **manual, human-executed,
backup-first procedure** — see
[`docs/migration-sage-dev.md`](docs/migration-sage-dev.md). It walks an
operator through inspecting `.sage/tickets/` data, deciding what is worth
preserving, and manually recreating the chosen items as real Enginery
work items; it never claims `sage-dev` command-interface compatibility.

## Installation

```bash
pip install enginery
enginery --version
enginery doctor
```

Requires Python 3.12+. Supports macOS and Linux.

## Links

- [`CHANGELOG.md`](CHANGELOG.md)
- [`docs/dependencies-v0.1.0.md`](docs/dependencies-v0.1.0.md) — dependency/license manifest
- [`docs/operations.md`](docs/operations.md) — operator guide
- [`docs/adapters.md`](docs/adapters.md) — adapter authoring guide
- [`docs/migration-sage-dev.md`](docs/migration-sage-dev.md) — manual migration guide
- [`docs/release-readiness-v0.1.0.md`](docs/release-readiness-v0.1.0.md) — pre-release Stage 1 gate and performance evidence
