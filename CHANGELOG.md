# Changelog

All notable changes to Enginery are documented in this file. Enginery does
not yet claim `1.0` API/schema stability; see [`RELEASE_NOTES.md`](RELEASE_NOTES.md)
for the current compatibility statement.

## [0.8.0] - 2026-10-27

### Fixed

- Repair publication expects the last earlier generation actually published according to the durable ledger, or an absent branch when review repairs precede the first publication. Unpublished intermediate generations no longer cause false remote conflicts; unexpected remote revisions remain protected by the publication lease.
- Before publication, actionable independent review findings and redacted `enginery feedback` now dispatch the next bounded implementation repair generation with actionable finding text in its worker task. Review repair context is deduplicated and capped at 32 KiB with an explicit truncation marker; feedback retains its 1 MiB input limit. Exhausted budgets and blocking findings still await human review; publication authority is unchanged.

### Added

- `enginery engine evidence --run-id R --verify` re-hashes every artifact referenced by the run without modifying evidence. It reports intact, missing or tampered artifacts, exits 0 when intact and `validation_failure` (12) otherwise; a missing database raises a missing-prerequisite error without creating it (#238).
- The pinned worker image includes CPython 3.12 and Enginery's hash-locked runtime dependencies alongside Debian Python 3.11.2. The Python-shebang `enginery-checkout` command runs the current candidate's `./src/enginery` for network-free isolated self-development validation and behavioral acceptance checks, without installing or modifying the checkout.
- Self-development requires the isolated runner for Enginery's own source project, keeping the installed runtime, operator configuration, ledger and credentials outside worker mounts. The standalone `scripts/enginery_runtime.py` installs confirmed wheel hashes into retained virtual environments, lists runtime receipts, and rolls back through an external shim only when all discovered or explicit ledgers fit the previous runtime's schema. See `docs/operations.md` for pause/drain, backup, upgrade and restore procedures.
- Trusted-local admission refuses unreadable, malformed or oversized existing `pyproject.toml` files with a project-identity diagnostic and a fix-or-use-isolated remedy; isolated admission does not read project metadata. The self-development guard also covers advancement of already-bound runs through `run`, `continue` and `engine next --advance`. Runtime rollback refusals name the previous executable's restore command, including the explicit unreadable-ledger override after operator pause/drain.

### Breaking

- Python import paths for the run-workflow modules are renamed by responsibility, without aliases: `enginery.workflows.stage1` → `enginery.workflows.run_lifecycle` and `enginery.workflows.stage1_runtime` → `enginery.workflows.node_executors`. Class and function names, CLI commands, persisted `stage1` identifiers and request digests are unchanged.
- The CLI implementation modules follow suit: `enginery.cli.stage1` → `enginery.cli.engine_runs`, `stage1_request` → `request_compiler`, `stage1_composition` → `run_composition`, `stage1_runner` → `runner_wiring`, `stage1_execution` → `execution_diagnostics`, and `enginery.cli.stage2` → `enginery.cli.stack_status`. Commands, options, JSON output and exit codes are unchanged.

## [0.7.0] - 2026-10-20

Current release line for isolated execution and the manual-first project workflow. No new workflow stage.

### Added

- `enginery engine build-request --runner trusted-local|isolated-oci` selects explicit execution boundaries. Isolated OCI execution separates worker and broker authority, host-side scoped push and model-access relay; `enginery runner custody-receipt` exposes custody. Trusted-local execution is not containment.
- Capability package closure locks (schema 2) bind every file's path, kind, hash, size and executable bit plus dependency pins and strict `capability.json`. `enginery capability inspect|preview|install|uninstall` supports typed project, user, pinned git and Armory sources with documented precedence.
- Per-package exact-template license/NOTICE screening; unknown licenses hold. Digest-bound policy-registry approvals, install hold exit 5 with actual digests, private verified command copies, argv/interpreter-only execution, shell-shebang rules, canonical JSON/schema bounds and bounded timeout/output. No automatic approval command.
- Tracked `.enginery/project.toml`, private `$XDG_CONFIG_HOME/enginery/user.toml`, packaged `standard`/`careful` profiles and factory defaults. Strict schema 1, bounded 1 MiB reads and actionable file/key/value diagnostics. Precedence is run > project > user > workflow > factory; hard constraints conjunct. Project policy cannot choose executable paths or Docker context and rejects credential-shaped values.
- `enginery init` and `uninstall` preview by default; `--apply` creates private ownership records or removes only unchanged owned files. User-owned conflicts exit 9; capability/harness holds are explicit. `preflight` checks prerequisites read-only with repairs; `explain` reports source chains, ownership and holds. Setup never silently changes credentials, services, MCP or global configuration.
- `collect [--dry-run]` reads open origin issues with canonical kind/risk labels, deduplication, workflow/harness/model/effort provenance and runner/budget basis. Reports `external_effects` and `local_writes`, including dry-read SQLite sidecars; no GitHub mutation. Intake writes only private cursor/index/lock; missing labels, authentication/configuration/capability or pause conditions hold exit 5 with repairs.
- `run OWNER/REPO#N [--once|--wait-seconds N] [--supersede]` binds stable `fr-…` identities to project, issue and snapshot through the existing coordinator. Drift holds; supersession needs safe-old-run proof. `status [RUN]` reports worker/container, settings, budget, holds, repairs, decisions and supersession. `continue RUN` reconciles interrupted worker/branch/PR operations without a new run. Project `pause`, `resume` and `drain` bind context and report released resources.
- Behavioral evidence categories distinguish worker claims, instrument observations and evaluated verdicts; PNG validation and base-bound operator `[acceptance]` checks enter evaluator custody. Missing checks hold `no_behavioral_checks`. Command, loopback service and digest-verified browser observers are explicit; unqualified browsers hold. Isolated command checks run network-none; service/browser checks hold `behavioral_observer_requires_isolation`.
- `calibrate RUN`, `check RUN`, terminal/private CSP HTML `review RUN`, repair-only `feedback RUN`, and authenticated `decide` with `[review_authority]` principals and one-use same-terminal challenges. Current-head evaluated pass precedes verify; repair is bounded with terminal exhaustion/build-mismatch guidance. Optional Lavish rendering has telemetry off.
- Repository-only qualification tooling: `scripts/run_factory_gate.py` scenarios `runner-boundary`, `runner-auth`, `setup`, `manual-path`, `behavioral-acceptance`, `review-authority`, `protocol` and `manual-bootstrap`; campaign binding/reuse flags; `release_consumer_smoke.py --install-receipt` schema-1 platform/artifact/hash/command-exit/version/outcome receipts. These scripts are not installed product commands.

### Changed

- Scoped `engine` and `operator` groups replace roadmap-shaped namespaces; project verbs are top-level. Host `doctor`, `adapter`, `runner`, and separate `outcome`, `gate`, `workspace`, `policy` and `capability` groups retain their scopes. The command consolidation itself preserves JSON key/type and exit-code contracts, persisted owners and historical schemas; emitted decision labels use `review|decide` / `engine review`. Separate execution-report changes are listed under Breaking.
- Catalog v3 separates accepted numeric executable ranges, tested versions and observed receipt versions; historical exact-schema semantics remain. `engine build-request`, `engine next` and `engine evidence` preserve that distinction.
- Git must be at least 2.45.1 or a supported patched 2.39.4/2.40.2/2.41.1/2.42.2/2.43.4/2.44.1 branch. Operator git configuration comes from a fixed non-repository directory; includeIf-only credential helpers are not carried over.
- Installed capability locks must equal retained pins; state paths are link-safe; uninstall revokes first without deleting the store, locks, ledger or evidence. Pinned Armory reads verify blob identities.
- GitHub evidence retains complete raw responses with bounded, pagination-proven collection, latest-review veto, classified failures and terminal failed outcomes. `status RUN --github-view` uses gh-axi for display only, never authority.

### Breaking

- Migrate `stage1 …` to `engine …`, including `stage1 watch` → `engine next`; `stage1 resume` is removed, not project dispatch `resume`. `stage2 status` → `engine status`. `ledger verify|rebuild-projections` → `operator verify|rebuild-projections`; raw `ledger backup|restore` are removed in favor of checked `operator backup|restore`. Removed paths fail as unknown commands (exit 2), without aliases.
- The never-published `factory` group becomes top-level project verbs: `factory doctor` → `preflight`, `factory resume` → `continue`, `factory pause --lift` → `resume`, and `factory explain RUN` → `status RUN`. The intra-train `enginery-operator` executable folds into `enginery operator`; neither `factory` nor that executable shipped in 0.6.0. Only the `enginery` console script is installed.
- `engine build-request` now compiles the labelled GitHub issue snapshot named by `--external-reference OWNER/REPO#N`, requiring working `gh` access and exactly one `enginery/work-kind/{issue,plan}` label plus one `enginery/risk/{low,medium}` label. The 0.6.0 values `--work-kind milestone|incident|factory_change` and `--risk-class high` have no label equivalent and are refused. Removed source/content flags: `--work-item-id`, `--source-provider`, `--work-kind`, `--source-snapshot-reference`, `--source-revision`, `--title`, `--objective`, `--acceptance-criterion`, `--constraint`, `--risk-class`. Move those inputs into the issue rather than merely renaming the old command. `--inapplicable-criterion` now indexes the issue's own acceptance criteria (0-based), not a supplied criterion list. `--implementation-no-cost-budget` is removed, and `--implementation-cost-budget` is always refused because no supported harness enforces a hard-dollar budget; use the enforced time budget.
- `engine next` and `engine evidence` JSON replace `execution.tested_host` with `execution.host`, separating accepted, tested and observed executable versions, and add `execution.runner`. Update consumers of the old host key; a tested version is not an observed version.
- Removed `--configuration-snapshot-digest` / `--configuration-snapshot-file`. Rebuild new requests with `engine build-request` to bind actual `effective_config` and its digest; `engine start` refuses absent/mismatched snapshots, violated constraints and changed sources/location. Already-started historical runs retain state.
- Legacy single-blob capability locks are read-only; re-resolve with explicit `--replace-legacy-lock`. Custom sources must replace `CapabilitySourcePort.fetch` with `fetch_closure`; `discover()` now returns `CapabilityListing`.
- `engine review` findings alone no longer authorize publication. Opening/protected publication requires current exact-subject authority through `decide`; `engine approve|reject` use it. Human-wait requeue remains refused (`human_wait_requeue_unsupported`), distinct from project dispatch `resume` and interrupted-run `continue`.

### Fixed

- Both adapters report cancellation even without a terminal marker and accept pinned harnesses' full supported event vocabularies; unknown events still fail closed.
- Isolated results use a plain filename; a missing copied path is reported directly instead of as an unreadable empty archive. Workers receive actual permitted single-command git/shell constraints and usable commit-message guidance.
- Combined commit-status evidence may omit creator without falsely holding valid evidence; PR titles use the issue title, not its body.
- Observer failures retain their original cause when macOS returns EPERM for an already-exited process group.

### Not part of this release

- Both harness tiers remain experimental. Live qualification used a designated synthetic qualification repository, proving factory mechanics, not usefulness on real projects or all platform/engine combinations. Effective effort remains UNKNOWN; a worker-reported model is not trusted enforcement. A Lima install receipt is not live Lima two-harness qualification.
- G4 remains deferred/fail-closed; no Stage 4, hosted/multi-tenant service, unattended authority, scheduled collection, self-development or upgrade claim. Merge, release and deployment remain separate decisions after a verified open PR.
- No dependency changes since 0.6.0. Publication hashes and published-artifact qualification belong to separate post-publication evidence, not preparation notes.

## [0.6.0] - 2026-10-13

Sixth release. `v0.6.0` makes the merged substrate-contract, execution-selection, and queue-liveness work available to consumers. It adds no workflow stage.

### Added

- A typed, closed check contract binds declared checks to `run_command` and `verify_evidence` workflow nodes (stable dotted check id, bound subject, an argv command contract or a named observer, and a stop or repair disposition per non-pass result). Stage 1's manifest is now `schema_version` 2 and binds `stage1.validate` and `stage1.verify`.
- A packaged, frozen acceptance baseline v1 (`acceptance-baseline-v1.toml`) with a loader that verifies its registered SHA-256 digest before parsing. A new Stage 1 run must bind checks identical to the baseline's declarations.
- A shell guard for Stage 1 validation commands: a command that launches a shell interpreter, directly or through a known launcher wrapper (`env`, `nice`, `timeout`, `sudo`, `uv run`, `npx`, and others), is rejected at `stage1 start`. It is an input guard, not a sandbox.
- Execution selection: a run's requested harness, model, and native effort are recorded with the layer that supplied each, resolved by a fixed precedence, and bound to a digest-frozen execution catalog (`execution-catalog-v1.toml`). Both supported hosts, OMP and Claude Code, are `experimental` in the catalog; this release claims no host qualification.
- `enginery stage1 build-request` gains `--model`, `--effort`, and `--strict-effective`. Aliases, bare OMP models, effort levels outside the host vocabulary, and a strict requirement with nothing pinned (or with `--effort`) are rejected with exit code 2 before any file is written.
- Native controls: the OMP adapter submits `--model` and `--thinking`; the Claude Code adapter submits `--model` and `--effort`. A task is rejected before launch on a wrong pin form, an out-of-vocabulary effort, a selection for another harness or catalog, or an installed host that differs from the tested version and protocol fingerprint.
- Execution receipts: `enginery stage1 watch` and `enginery stage1 evidence` print an `execution` block that keeps requested, selected, submitted, and effective controls apart, with the support tier, catalog identity, tested host fingerprint, and holds. Effective effort is reported as unknown on both hosts; a requested value is never presented as an effective one.
- Queued Stage 1 implementation work is redispatched: `stage1 watch --advance` handles a new `dispatch_queued` action that dispatches a durably queued node exactly once when capacity frees, or records why it stayed queued (`global_capacity`, `repository_capacity`, `dependencies`, `dependency_failed`, `requirements`, `capacity_reserved_for_other_run`, `execution_hold`). Capacity is granted oldest-queued first from a durable queue time.
- Worker liveness and identity: persisted worker identity binds the stable machine and boot identifiers, so a dead worker, a reused pid, a reboot, a live worker whose coordinator lapsed, a foreign host, and an unverifiable record are told apart. `stage1 watch` reports `lease_epoch_active` and the identity evidence for running nodes.
- `docs/operations.md` documents queued work, wakeups, worker recovery, and the execution block; `scripts/fault_inject_workers.py` gains real-execution lifecycle scenarios for queued redispatch, restart, stale coordinators, cancellation, foreign hosts, killed workers, and reboots.

### Changed

- Public documentation is reconciled with the shipped product: `v0.5.0` is stated as published, the proposed factory command family is labelled proposed and not shipped (the CLI has no `enginery factory` command), and merge authority is described as the shipped policy rather than an unconditional claim.
- A new `stage1 build-request` defaults the implementation attempt id to `<run-id>-implement-0` and repair attempts to `<run-id>-implement-N`, so a second run on a shared ledger no longer fails its lease. An explicit `--implementation-attempt-id` is honored and persisted runs keep their ids; a run id too long to carry the derived ids exits 2.
- Cancelling a running worker is fenced by the active coordinator epoch rather than by the epoch that issued its lease, so it works after a coordinator restart. Cancel is retryable from a recorded termination request, termination waits for the whole process group with bounded SIGTERM then SIGKILL escalation, and an operator's cancel wins over a late result.
- A worker proven dead (same host and boot, start identity absent, no result) is failed on the next advance with its identity evidence and frees its slot. A reused pid or a reboot is reconciled and never signalled; a different machine or an unreadable identity holds and keeps its slot.
- Launched workers no longer inherit the coordinator's stdin, stdout, or stderr, so `stage1 watch --advance | reader` returns when the command does. Worker stdout is discarded; worker stderr goes to a private per-attempt log under the artifact root.
- Worker start identity is read under a fixed time zone and locale so launching, watching, and cancelling from shells with different `TZ` or `LANG` agree.
- A run whose bound execution catalog digest no longer matches, or, for a run that selected a native control, whose installed host fingerprint no longer matches, is held with a diagnostic, every service action is refused, and the original binding is retained.
- `enginery stage1 watch` and `enginery stage1 evidence` JSON output gains `execution` and worker and queue diagnostic fields. The additions are additive; existing fields are unchanged.

### Breaking

- Requests written by `v0.5.0`'s `enginery stage1 build-request` (manifest schema 1) are rejected by `enginery stage1 start`. Rebuild them with the current `enginery stage1 build-request`. A run already started from such a request is read and resumed unchanged; persisted schema 1 manifests keep their content digest.
- `ClaudeCodeAdapterConfig.model` is removed. The model and effort a run requests come only from the per-task execution selection; when it names none, no flag is passed and Claude Code resolves its own default.
- The request state of a new Stage 1 run includes its execution selection, so a rebuilt request has a different digest from one built for the same inputs by `v0.5.0`. Requests persisted before this change keep their exact digest and decode as a bare host default.
- `enginery stage1 start` rejects a new run whose request validation commands launch a shell interpreter.
- `Stage1RunRequest` gains a required `execution_selection` and `HarnessTask` a required `selection` (`ExecutionSelection`). Python callers that build them directly must pass one, for example from `resolve_execution_selection(...)`; see `docs/examples.md`.
- The default implementation attempt id of a new request changes from `implement-0` to `<run-id>-implement-0` (repair attempts are `<run-id>-implement-N`). Scripts that hard-code `implement-0` for a new run must use the derived id or pass `--implementation-attempt-id`.

### Not part of this release

- No isolated runner, live two-host qualification, or factory command is part of this release. Both agent hosts remain `experimental`, trusted-local execution is not containment, and no hosted or multi-tenant claim is made.
- Gate G4 and Stage 4 are unchanged: `enginery gate status --gate G4` remains fail-closed, no G4 passage is recorded, and Stage 4 has no committed date. The acceptance baseline v1 added in this release covers only current supported behavior; it adds no G4 or Stage 4 acceptance.
- No dependency changes since `v0.5.0`.

## [0.5.0] - 2026-08-11

Fifth public release. `v0.5.0` makes M24's measurable, fail-closed G4 evidence surface available to consumers. It adds no workflow stage.

### Added

- GitHub source snapshots bind exactly one declared `enginery/work-kind/{issue,plan}` label and one declared `enginery/risk/{low,medium}` label to the source revision and digest. Missing, unknown, duplicate, conflicting, or case-variant classifications reject before mutation.
- Classified `issue` and `plan` work can enter Stage 1. Low-risk work follows the existing direct path; medium-risk work stops for human approval.
- Gate G4 supports a schema-2 authority roster mapping durable principals to immutable GitHub numeric user IDs, with login names retained only as diagnostics.
- `enginery gate record-g4-deficiency` records an immutable recurring-deficiency finding against eligible classified runs, and `enginery gate record-g4-deficiency-evidence` verifies its merged GitHub evidence pull request and exact-head approvals.
- `enginery gate status --gate G4 --json` reports the enriched durable evidence state and remains fail-closed for every missing, stale, self-approved, unverifiable, or insufficient condition.

### Not part of this release

- This release adds no workflow stage. Stage 1, Stage 2, and Stage 3 remain the shipped workflow surface.
- Gate G4 still requires real multi-repository, dual-human numeric-identity, intervention, outcome, and deficiency evidence before M14b/M15. This release does not create that evidence or report a G4 pass.
- Stage 4 self-improvement, cohort/replay, candidate evaluation, canary, and promotion remain unimplemented and gate-deferred with no committed date.

## [0.4.0] - 2026-08-04

Fourth public release. `v0.4.0` makes the already-merged operator surface available to consumers. It adds no workflow stage.

### Added

- `enginery gate status --gate G4` reports the registered G4 entry conditions from durable ledger state and configured floors, including fail-closed `unmeasured` results where the evidence cannot support a pass.
- `enginery stage1 build-request` writes a validated Stage 1 request document from explicit command-line inputs.
- `enginery workspace inspect` reports durable workspace reservations, and `enginery workspace release` requires the existing fenced release proof before releasing a reservation.
- `enginery adapter doctor --json` includes the Stage 2 GitHub Release and PyPI brokers plus the Stage 3 controlled-local-service deployment broker alongside the existing adapter probes.

### Not part of this release

- This release adds no workflow stage. Stage 1, Stage 2, and Stage 3 remain the shipped workflow surface.
- Self-improvement is not implemented. Stage 4 (governed factory self-improvement) remains gate-deferred behind G4 with no committed date.
- M20–M23's repository tooling and published evidence are intentionally not part of the distribution.

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
