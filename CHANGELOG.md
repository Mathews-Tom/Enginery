# Changelog

All notable changes to Enginery are documented in this file. Enginery does
not yet claim `1.0` API/schema stability; see [`RELEASE_NOTES.md`](RELEASE_NOTES.md)
for the current compatibility statement.

## [0.3.0] - 2026-07-22

Third public release. Layered on `v0.1.0`'s Stage 1 and `v0.2.0`'s
Stage 2, this release ships Stage 3 (production incident to verified
hotfix and rollback) against a controlled local service.

### Added

- A closed-lifecycle `Incident` domain aggregate (severity/authority
  mapping, release lineage, containment, and falsifiable reproduction
  records) and an `IncidentService` that ingests, classifies, binds
  release lineage, attempts reproduction, deploys, observes, rolls
  back, and records follow-up work, entirely through the durable
  ledger.
- A fixed-broker hotfix workflow (`enginery.incidents.hotfix`):
  git-worktree creation at the affected revision, minimal repair
  application, and non-vacuous regression evidence -- a repair is
  only accepted after it is proven to fail on the unfixed revision
  and pass on the repaired one, reusing the existing independent
  review routing unchanged.
- A real, stdlib-only controlled local HTTP service fixture and a
  real `DeploymentPort` implementation
  (`enginery.adapters.local_service.LocalServiceDeploymentAdapter`)
  that starts, stops, and version-swaps a genuine subprocess -- never
  a canned or simulated receipt.
- Two independently policy-approved, short-lived deployment authority
  grants (`deployment.execute`, `deployment.rollback`) with durable
  authority records, so no agent or arbitrary command ever holds a
  standing deployment credential.
- A cumulative Stage 1+2+3 restart/replay gate
  (`full_system_gate.py --stages 1,2,3`), extending the Stage 1+2
  gate with a real, ledger-backed Stage 3 leg that proves the
  incident's durable state -- including its terminal `rolled_back`
  outcome -- survives a coordinator restart.

### Not part of this release

- Self-improvement (candidate evaluation, canary rollout, and
  promotion) is not implemented. Stage 4 (governed factory
  self-improvement) remains gate-deferred behind a data-threshold
  entry gate with no committed date.
- There is no hosted, multi-tenant, or browser-dashboard surface.
- Windows is not supported. Process supervision, cancellation, and
  recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace separation, not
  hostile-code containment.
- The controlled deployment target in this release is a local
  fixture HTTP service, never real production infrastructure or a
  cloud destination.

## [0.2.0] - 2026-07-21

Second public release. Layered on `v0.1.0`'s Stage 1 capability, this
release ships Stage 2 (plan to verified released version) plus two
cross-cutting additions the Stage 2 train depended on: harness
neutrality proved against a second coding-agent harness, and
capability locking with provenance verification.

### Added

- A second coding-agent harness adapter (Claude Code), proving the
  harness port contract is not shaped around any one provider: the
  same normalized task, event, cancellation, artifact, and evidence
  fixture passes against both OMP and Claude Code.
- Capability locking and provenance verification: a capability
  lockfile, a pinned-key signature-verification primitive, an optional
  Armory capability-registry adapter (disabled by default), and a hard
  rule requiring interactive human exact-digest approval before a
  run-introduced or changed capability executes.
- Development-plan ingestion, dependency-safe child-run scheduling,
  and stacked-branch topology tracking: a plan schema, milestone
  dependency-graph validation, a plan-to-child-run process manager,
  and a stack-evidence projection covering linear, parallel, diamond,
  failed, and resumed plan shapes.
- A complete Stage 2 (plan to verified released version) lifecycle:
  root-to-leaf stack merging under the merge-ready contract's
  double-read discipline, policy-gated version/changelog preparation,
  fixed-broker wheel/sdist build, GitHub Release and PyPI publication
  adapters, ambiguous-publication reconciliation, and destination
  verification. Proven end to end by live-publishing a disposable
  fixture distribution through both real destinations.
- A cumulative Stage 1+2 restart/replay gate, extending the Stage 1
  gate with a local, in-process, no-network proof of Stage 2's full
  merge -> prepare -> build -> publish -> verify sequence, including
  publish-side idempotent-replay verification after a simulated
  coordinator restart.

### Not part of this release

- Stage 3 (incident to hotfix and rollback) ships in the `v0.3.0`
  release train.
- Stage 4 (governed factory self-improvement) is gate-deferred behind
  a data-threshold entry gate with no committed date; it does not
  exist in this release.
- Self-improvement (candidate evaluation, canary rollout, and
  promotion) does not exist in this release.
- Windows is not supported. Process supervision, cancellation, and
  recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace separation, not
  hostile-code containment.
- There is no hosted, multi-tenant, or browser-dashboard surface.

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
