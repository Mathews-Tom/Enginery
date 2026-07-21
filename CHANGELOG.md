# Changelog

All notable changes to Enginery are documented in this file. Enginery does
not yet claim `1.0` API/schema stability; see [`RELEASE_NOTES.md`](RELEASE_NOTES.md)
for the current compatibility statement.

## [0.1.0] - 2026-07-21

Initial public release. This is a **Stage 1-only** release: a
coordinator-owned, durable issue-to-merge-ready-pull-request workflow,
shipped together with the raw outcome-observation schema so runs emit
versioned observations from day one.

### Added

- Domain, ledger, and policy foundations: immutable work-item, run, and
  node-attempt lifecycles; a durable SQLite event ledger with a
  transactional inbox/outbox, projections, backup, restore, and
  fail-closed schema migration; a named-action policy engine with
  default-deny evaluation, human-approval gates, and independently
  adversarial-tested hard rules.
- A coordinator-owned runtime with fenced leases, exclusive git-worktree
  workspaces, process-group supervision, and crash/restart recovery that
  never duplicates an external side effect.
- GitHub and local work-ledger, source-control, and validation adapters,
  with redacted diagnostics and four-result reconciliation on every
  external side effect.
- A complete Stage 1 (issue to merge-ready pull request) lifecycle:
  qualification, implementation dispatch, validation, independent review,
  bounded repair, pull-request creation, exact-head CI verification,
  merge-ready evidence, and cancellation/resume. Proven end to end against
  a real allowlisted GitHub repository and a real coding-agent harness,
  and shown to survive a coordinator restart without duplicating any
  external side effect.
- A versioned raw outcome-observation schema and Stage 1 adapters, so
  every run's later external state (merged, closed, reopened) is captured
  with an explicit, fail-closed completeness metric that cannot be
  inflated by suppressing or delaying attribution.
- Operator, adapter, and migration documentation: install/config/health
  checks, the real CLI command surface, recovery semantics, backup and
  restore, security limits, a manual backup-first `sage-dev` migration
  guide, and two worked end-to-end examples.
- A cumulative Stage 1 restart/replay gate and a measured local
  performance baseline used for regression detection.

### Not part of this release

- Stage 2 (plan to verified release), Stage 3 (incident to hotfix and
  rollback), and Stage 4 (governed factory self-improvement) are not
  released capabilities of `v0.1.0`. Stage 2 and Stage 3 ship in later
  release trains (`v0.2.0` and `v0.3.0`); Stage 4 is additionally
  gate-deferred behind a data-threshold entry gate with no committed
  date.
- Self-improvement (candidate evaluation, canary rollout, and promotion)
  does not exist in this release.
- The repository already contains early, forward-looking groundwork for
  later trains (a second coding-agent harness adapter, capability
  locking and provenance, a read-only Stage 2 stack-inspection command,
  and plan-to-child-run scheduling). None of it is a supported or
  documented feature of `v0.1.0`; it is undocumented here and will be
  described in its own release train's changelog entry when that train
  ships.
- Windows is not supported. Process supervision, cancellation, and
  recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace separation, not hostile-code
  containment.
- There is no hosted, multi-tenant, or browser-dashboard surface.
