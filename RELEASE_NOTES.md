# Enginery `v0.2.0` Release Notes

**This release adds Stage 2 on top of `v0.1.0`'s Stage 1.** `v0.2.0`
ships second-harness neutrality (a Claude Code adapter alongside OMP),
capability locking and provenance verification, development-plan
ingestion with dependency-safe child-run scheduling, and the complete
Stage 2 (plan to verified released version) lifecycle: root-to-leaf
stack merge, policy-gated version/changelog preparation, fixed-broker
build, and real GitHub Release + PyPI publication. Stage 3 (incident to
hotfix and rollback) ships in the next release train, `v0.3.0`. Stage 4
(governed factory self-improvement) is additionally **gate-deferred**:
its milestones may not start until a data-threshold entry gate passes
(sufficient completed-run and intervention volume across at least two
workflow types and risk classes, an outcome-capture completeness floor,
at least one recurring evidence-backed workflow deficiency, corpus
diversity beyond a single repository, and a second registered human
principal). That gate has **no committed date** and is evaluated on a
review cadence, never by elapsed time. Self-improvement does not exist
in this release.

## What ships in `v0.2.0`

Carried over from `v0.1.0`:

- A durable event ledger (SQLite), transactional inbox/outbox,
  projections, backup/restore, and fail-closed schema migration.
- A named-action policy engine with default-deny evaluation and
  independently adversarial-tested hard rules.
- A coordinator-owned runtime: fenced leases, exclusive git-worktree
  workspaces, process-group supervision, and crash/restart recovery
  that never duplicates an external side effect.
- The complete Stage 1 lifecycle — qualify, implement, validate,
  review, bounded repair, open a pull request, wait for exact-head CI,
  verify merge-ready evidence, cancel/resume — proven against a real
  GitHub repository and a real coding-agent harness.
- A versioned raw outcome-observation schema and Stage 1 adapters, with
  a fail-closed completeness metric.

New in `v0.2.0`:

- **Second-harness neutrality.** A Claude Code headless adapter passes
  the same normalized task, event, cancellation, artifact, and evidence
  fixture as OMP, proving the harness contract is not shaped around any
  one provider. Neither provider requires a provider-named domain
  field, and missing harness installations fail `doctor` clearly with
  no silent fallback.
- **Capability locking and provenance verification.** A capability
  lockfile binds an exact resolved digest per run; a pinned-key
  signature-verification primitive distinguishes authenticated from
  unauthenticated provenance; an optional, disabled-by-default Armory
  capability-registry adapter is available; and a closed hard rule
  requires interactive human exact-digest approval before any
  run-introduced or changed capability executes. The engine works with
  Armory disabled.
- **Development-plan ingestion and stacked child-run scheduling.** A
  plan schema, milestone dependency-graph cycle/unresolved-dependency
  validation, a plan-to-child-run process manager, and a stack-topology
  model with an evidence projection — proven against linear, parallel,
  diamond, failed, and resumed plan shapes with zero duplicate child
  runs after a coordinator restart.
- **The complete Stage 2 lifecycle.** Root-to-leaf stack merge under
  the merge-ready contract's double-read discipline; policy-gated
  version/changelog preparation that cannot begin before every
  constituent milestone has merged; a fixed-broker wheel/sdist build;
  GitHub Release and PyPI publication adapters with ambiguous-publish
  reconciliation; and destination verification confirming the
  published artifact digest matches the release manifest. Proven end
  to end by live-publishing a disposable fixture distribution
  (`enginery-stage2-fixture`) through both real destinations.
- **A cumulative Stage 1+2 restart/replay gate.** Extends the Stage 1
  gate with a local, in-process, no-network proof of Stage 2's full
  merge → prepare → build → publish → verify sequence, including
  publish-side idempotent-replay verification after a simulated
  coordinator restart.

See [`CHANGELOG.md`](CHANGELOG.md) for the itemized list, including the
carried-forward `v0.1.0` entry.

## Compatibility and known limitations

- `v0.2.0` publishes schema and API versions but makes **no `1.0`
  stability promise**. Runs bind adapter/version fingerprints and block
  a silent resume under different adapter behavior.
- **Stage 3 (incident to hotfix and rollback) is not implemented.** It
  ships in the `v0.3.0` release train.
- **Stage 4 (governed factory self-improvement) is not implemented and
  is gate-deferred** behind a data-threshold entry gate with no
  committed date, per the criteria above.
- Self-improvement (candidate evaluation, canary rollout, and
  promotion) does not exist in this release.
- There is no hosted or multi-tenant service, no organization RBAC, no
  browser dashboard, and no interactive TUI.
- **Windows is not supported.** Process supervision, cancellation, and
  recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace **separation**, not
  hostile-code containment: it does not prevent a process from reaching
  the operator's account, filesystem, network, or keychain.
- Medium- and high-risk work requires human final review; there is no
  autonomous unattended-merge mode.
- The Armory capability-registry adapter reports every external
  capability as `unauthenticated` provenance today: Armory itself does
  not yet publish per-package signatures, so no external capability can
  reach `authenticated` status through it alone.

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
- [`docs/dependencies-v0.2.0.md`](docs/dependencies-v0.2.0.md) — dependency/license manifest
- [`docs/operations.md`](docs/operations.md) — operator guide
- [`docs/adapters.md`](docs/adapters.md) — adapter authoring guide
- [`docs/migration-sage-dev.md`](docs/migration-sage-dev.md) — manual migration guide
- [`docs/release-readiness-v0.1.0.md`](docs/release-readiness-v0.1.0.md) — `v0.1.0` pre-release Stage 1 gate and performance evidence
