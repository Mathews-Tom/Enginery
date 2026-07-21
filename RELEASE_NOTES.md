# Enginery `v0.3.0` Release Notes

**This release adds Stage 3 (incident to hotfix and rollback) on top of
`v0.1.0`'s Stage 1 and `v0.2.0`'s Stage 2.** `v0.3.0` ships a real
production-incident workflow against a controlled local service:
ingest, classify, bind release lineage, attempt reproduction, apply a
minimal hotfix under non-vacuous regression evidence, deploy under an
independently policy-approved short-lived grant, observe the deployed
revision, roll back under a second, independently approved grant when
observation is unhealthy, and confirm the prior revision is genuinely
restored. Stage 4 (governed factory self-improvement) is additionally
**gate-deferred**: its milestones may not start until a data-threshold
entry gate passes (sufficient completed-run and intervention volume
across at least two workflow types and risk classes, an
outcome-capture completeness floor, at least one recurring
evidence-backed workflow deficiency, corpus diversity beyond a single
repository, and a second registered human principal). That gate has
**no committed date** and is evaluated on a review cadence, never by
elapsed time. **Self-improvement does not exist in this release.**

## What ships in `v0.3.0`

Carried over from `v0.1.0` and `v0.2.0`:

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
- A second coding-agent harness adapter (Claude Code), proving the
  harness port contract is not shaped around any one provider.
- Capability locking and provenance verification, with an optional,
  disabled-by-default Armory capability-registry adapter.
- Development-plan ingestion, dependency-safe child-run scheduling,
  and stacked-branch topology tracking.
- The complete Stage 2 lifecycle — root-to-leaf stack merge, policy-
  gated version/changelog preparation, fixed-broker build, and real
  GitHub Release + PyPI publication.
- A versioned raw outcome-observation schema and Stage 1 adapters,
  with a fail-closed completeness metric.

New in `v0.3.0`:

- **A closed-lifecycle incident domain aggregate.** `Incident`
  (fifteen closed states), severity-to-authority mapping, `ReleaseLineage`
  binding, `ContainmentAction`, and falsifiable `ReproductionRecord`/
  `ReproductionOutcome` types. Unreproduced incidents are never labeled
  reproduced: reproduction is only recorded from an actually-executed
  check, never a hand-typed claim.
- **A fixed-broker hotfix workflow.** Git-worktree creation at the
  affected release revision, minimal repair application, and
  non-vacuous regression evidence — a repair is accepted only after
  the same real check is proven to fail on the unfixed revision and
  pass on the repaired one. Reuses the existing independent review
  routing unchanged; the emergency pull request never expands scope
  beyond the minimal fix.
- **A real controlled local service and deployment broker.** A
  stdlib-only local HTTP service fixture and a real `DeploymentPort`
  implementation (`LocalServiceDeploymentAdapter`) that starts, stops,
  and version-swaps a genuine subprocess and polls its live `/health`
  and `/version` endpoints — never a canned or simulated receipt.
- **Independently approved, short-lived deployment authority.**
  `deployment.execute` and `deployment.rollback` are separate
  hard-required-human policy actions; a deployment approval never
  satisfies the rollback requirement. Every grant and its outcome are
  recorded as a durable `DeploymentAuthorityRecord`. No agent or
  arbitrary command ever holds a standing deployment credential.
- **Separate follow-up work, never scope creep.** Deployment-health
  findings discovered during an incident are recorded as an
  independent follow-up work item, never merged into the emergency
  hotfix's own scope.
- **A cumulative Stage 1+2+3 restart/replay gate.** Extends the
  Stage 1+2 gate with a real, ledger-backed Stage 3 leg that drives
  the same `IncidentService`/`LocalServiceDeploymentAdapter`/hotfix
  code paths the live gate exercises, proving the incident's durable
  state — including its terminal `rolled_back` outcome and
  authority-record count — survives a coordinator restart.

See [`CHANGELOG.md`](CHANGELOG.md) for the itemized list, including the
carried-forward `v0.1.0` and `v0.2.0` entries.

## Compatibility and known limitations

- `v0.3.0` publishes schema and API versions but makes **no `1.0`
  stability promise**. Runs bind adapter/version fingerprints and block
  a silent resume under different adapter behavior.
- **Self-improvement is not yet implemented.** Stage 4 (governed
  factory self-improvement) is **gate-deferred** behind a
  data-threshold entry gate with no committed date, per the criteria
  above. Candidate evaluation, canary rollout, and promotion do not
  exist in this release.
- The controlled deployment target in this release is a **local
  fixture HTTP service**, never real production infrastructure, a
  container, or a cloud destination.
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
- [`docs/dependencies-v0.3.0.md`](docs/dependencies-v0.3.0.md) — dependency/license manifest
- [`docs/operations.md`](docs/operations.md) — operator guide
- [`docs/adapters.md`](docs/adapters.md) — adapter authoring guide
- [`docs/migration-sage-dev.md`](docs/migration-sage-dev.md) — manual migration guide
- [`docs/release-readiness-v0.1.0.md`](docs/release-readiness-v0.1.0.md) — `v0.1.0` pre-release Stage 1 gate and performance evidence
