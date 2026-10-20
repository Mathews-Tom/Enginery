# Enginery `v0.7.0` Release Notes

## Breaking changes and migration

This is a clean CLI cutover, with no moved-command aliases. Removed command paths fail as unknown commands with exit 2; update automation before upgrading.

| Previous spelling | Use in 0.7.0 |
|---|---|
| `enginery stage1 …` | `enginery engine …` |
| `enginery stage1 build-request …` | `enginery engine build-request --external-reference OWNER/REPO#N …`; rebuild from the labelled GitHub issue snapshot with working `gh` access, not the removed source/content flags or dollar-budget controls listed below. |
| `enginery stage1 watch …` | `enginery engine next …` |
| `enginery stage1 resume …` | Removed. Human-wait requeue remains refused; it is not project dispatch `resume` or interrupted-run `continue`. |
| `enginery stage2 status …` | `enginery engine status …` (engine-ledger stack status, not project status) |
| `enginery ledger verify …` / `rebuild-projections …` | `enginery operator verify …` / `rebuild-projections …` |
| Raw `enginery ledger backup …` / `restore …` | Removed. Use checked `enginery operator backup …` / `restore …`; these are verified recovery operations, not raw-backup shims. |

The intra-train `factory` group and `enginery-operator` executable **never shipped in 0.6.0**. Their consolidation still matters to source-checkout users: project verbs are now top-level, `factory doctor` becomes `preflight`, `factory resume` becomes `continue`, `factory pause --lift` becomes project `resume`, and `factory explain RUN` becomes `status RUN`. The separate executable is folded into `enginery operator`; the distribution installs exactly one console script, `enginery`.

Additional compatibility changes:

- **Build requests from the labelled issue, not caller-supplied source content.** `engine build-request --external-reference OWNER/REPO#N` fetches the GitHub issue snapshot through `gh`; working access and exactly one `enginery/work-kind/{issue,plan}` label plus one `enginery/risk/{low,medium}` label are required. The 0.6.0 values `--work-kind milestone|incident|factory_change` and `--risk-class high` have no label equivalent and are refused. Removed flags: `--work-item-id`, `--source-provider`, `--work-kind`, `--source-snapshot-reference`, `--source-revision`, `--title`, `--objective`, `--acceptance-criterion`, `--constraint`, `--risk-class`. Maintain content and classification on the source issue instead. `--inapplicable-criterion` now indexes the issue's own acceptance criteria (0-based), not a caller-supplied criterion list. `--implementation-no-cost-budget` is removed; `--implementation-cost-budget` is always refused because no supported harness enforces a hard-dollar budget. Use the enforced time budget instead.
- **Migrate execution JSON readers.** In `engine next` and `engine evidence`, `execution.tested_host` is replaced by `execution.host`, which separates accepted, tested and observed executable versions; `execution.runner` is also added. Update readers of the removed key and do not treat a tested version as an observed one.
- **Rebuild new requests with `engine build-request`.** The removed `--configuration-snapshot-digest` and `--configuration-snapshot-file` flags are replaced by binding the actual `effective_config` snapshot and its digest. `engine start` refuses absent/mismatched snapshots, violated bound constraints, and changed configuration sources or location. Already-started historical runs keep their state.
- **Re-resolve legacy capability locks.** Full package closures use lock schema 2. Legacy single-blob locks are read-only; replacing them requires explicit `--replace-legacy-lock`. Custom library sources must migrate `CapabilitySourcePort.fetch` to `fetch_closure`; `discover()` returns `CapabilityListing`.
- **Findings are not publication authority.** `engine review` alone no longer authorizes opening/protected publication. Configure `[review_authority]` principals and use `decide` with a one-use same-terminal challenge for the exact current subject; `engine approve|reject` use this path. Human-wait requeue remains refused with `human_wait_requeue_unsupported`.
- The command consolidation itself preserves JSON key/type and exit-code contracts; the separate execution-report changes are described above. Emitted `next_decision.action` labels use `review|decide` / `engine review`; persisted owners `factory` and `enginery-operator`, historical schemas, stored runs and old evidence command text are unchanged. There is no `1.0` API/schema stability promise.

## What ships in 0.7.0

### Isolated execution and explicit authority

`enginery engine build-request --runner trusted-local|isolated-oci` selects a non-containing trusted-local runner or an explicit isolated OCI profile. Isolated execution separates worker/broker authority, host-side scoped push and model-access relay. `enginery runner custody-receipt` exposes the custody binding; agent-free recovery uses `enginery operator`. Operator git configuration is read from a fixed non-repository directory, and includeIf-only credential helpers are not carried over. Trusted-local worktree separation is **not hostile-code containment**.

Catalog v3 distinguishes strict numeric accepted executable ranges from tested versions, preserving historical exact-schema semantics. Request provenance and JSON execution receipts keep accepted, tested and observed versions separate through `engine build-request`, `engine next` and `engine evidence`; acceptance of a version range is not a support-tier promotion.

### Verified capability packages

Closure locks bind every file's path, kind, hash, size and executable bit, dependency pins and strict `capability.json`. `enginery capability inspect|preview|install|uninstall` supports typed project/user/git/Armory sources, pinned revisions and documented precedence. Source options include `--project-source REPO@COMMIT`, `--project-subdir`, `--user-source`, `--git-source REPO@COMMIT`, `--git-subdir`, `--armory`, and `--armory-revision`.

Every source undergoes exact-template per-package license/NOTICE screening; unknown licenses hold. Policy-registry approvals bind actual digests, which install hold exit 5 reports; there is no automatic approval command. Execution uses private verified command copies, argv/interpreter-only boundaries, shell-shebang rules, the supported schema subset/canonical JSON, and bounded timeout/output. Installed locks must equal retained pins, state paths are link-safe, and pinned Armory reads verify blob identities. Uninstall revokes first without deleting the capability store, lockfile, ledger or evidence.

Git requires at least 2.45.1, or a supported patched 2.39.4/2.40.2/2.41.1/2.42.2/2.43.4/2.44.1 branch.

### Layered configuration and preview-first setup

Tracked `.enginery/project.toml`, private `$XDG_CONFIG_HOME/enginery/user.toml`, packaged `standard`/`careful` workflow profiles and factory defaults use strict schema 1 and bounded 1 MiB reads. Unknown keys/types/profiles produce file/key/allowed-value diagnostics with escaped controls. Precedence is run > project > user > workflow > factory; hard constraints conjunct across layers. `engine build-request` flags form the run layer; runner choice may come from project policy, but executable paths and Docker context may not. Credential-shaped project values are refused.

`init` previews by default and applies only with `--apply`, recording ownership in private project state; user-owned conflicts exit 9 and capability/harness holds are explicit. `preflight` checks prerequisites lazily/read-only with exact repairs; `explain` shows per-field source chains, ownership and holds. `uninstall` previews by default and removes only unchanged owned files, never user preferences, auth, ledger, evidence or capability store. Setup does not silently change credentials, OS services, MCP or global configuration.

### Manual project journey

- `collect [--dry-run]` reads open origin-repository issues with deterministic kind/risk labels, deduplication, workflow profile, compiled harness/model/effort provenance, runner and budget basis. It discloses `external_effects` and `local_writes`; GitHub mutations remain zero. Non-dry intake writes only private cursor/index/lock; dry-read SQLite sidecars are disclosed. Missing/conflicting labels, source/auth/config/capability or pause conditions hold exit 5 with repairs.
- `run OWNER/REPO#N [--once|--wait-seconds N] [--supersede]` binds a stable `fr-…` identity to project, canonical issue and snapshot through the shared compiler and existing Stage 1 coordinator. The ledger is private (historical stored owner `factory`); explicit `engine --database` operations can inspect it. Source drift holds; supersession needs safe-old-run proof.
- `status [RUN]` shows current node, worker/container identity, settings/sources, runner/budget, holds/repairs, next decision and supersession. Plain `explain` remains configuration explanation.
- `continue RUN` reconciles worker/branch/PR operations by stable operation identity, without making a new request, workspace or run. It is not human-wait requeue.
- Project `pause [--reason]`, `resume` and `drain (--wait-seconds N|--stop)` use the bound context and existing operator drain path with explicit resource-release reports.

The local journey ends at a verified open PR. Merge, release and deployment remain separate decisions. Host `doctor`, `adapter` (including `adapter doctor`) and `runner` retain their scopes; `engine` contains build-request/start/next/review/approve/reject/cancel/evidence/status, and `operator` contains status/pause/resume/drain/select-runner/backup/restore/verify/rebuild-projections. Separate `outcome`, `gate`, `workspace`, `policy explain` and `capability` groups remain.

### Behavioral acceptance and authenticated review

Evidence distinguishes `worker_claim`, `instrument_observation` and `evaluated_verdict`, with PNG media validation. Operator `[acceptance]` checks bind from the base revision into evaluator custody; absent checks hold `no_behavioral_checks`. Observers cover command/CLI, loopback services and digest-verified headless browsers; unqualified browsers hold explicitly. In isolated runs command checks execute network-none, while service/browser checks hold `behavioral_observer_requires_isolation`.

`calibrate RUN` and `check RUN` require a current-head passing evaluated verdict before verify; repair is bounded, with terminal exhaustion/build-mismatch and new-run guidance. `review RUN` renders terminal/private CSP HTML; `feedback RUN` records repair findings only, never approval. Optional Lavish presentation disables telemetry. `decide` uses registered principals, new ledger tables and one-use same-terminal challenges for exact-subject authority.

GitHub evidence retains complete raw responses with bounded, pagination-proven collection, latest-review veto, failure classification and terminal failed outcomes. `status RUN --github-view` uses gh-axi solely for display, not authority.

### Reliability fixes

- Both adapters report cancellation even without a terminal marker and accept the pinned harnesses' full supported event vocabularies; unknown events still fail closed.
- Isolated workers use a plain result filename. Missing copied paths are reported directly rather than as unreadable empty archives.
- Workers receive their actual permitted single-command git/shell constraints and usable commit-message guidance.
- Valid combined commit-status evidence can omit creator without a false hold. PR titles use the issue title, not its body.
- On macOS, EPERM for an already-exited process group no longer masks the observer's original failure as unavailable tooling.

## Repository-only qualification and release tooling

The wheel packages `src/enginery`, not `scripts/`. Repository/operator tooling includes `scripts/run_factory_gate.py` scenarios `runner-boundary`, `runner-auth`, `setup`, `manual-path`, `behavioral-acceptance`, `review-authority`, `protocol` and `manual-bootstrap`; `--campaign-id`, `--campaign-config` and `--reuse-evidence` bind qualification campaigns. These are not installed `enginery` workflow commands.

`release_consumer_smoke.py --install-receipt` records schema-1 platform/artifact/hash/command-exit records, installed version and outcome. Consumer gates check the consolidated surface, reject removed spellings, enforce the single console script and run the 20-assertion manual-path journey with the installed Python, removed `PYTHONPATH` and consumer-venv import identity. Missing source or engine proof is NOT RUN → INCOMPLETE, never PASS. This local stand-in workflow is installed-product proof, not hosted-model qualification. Publication hashes and published-artifact consumer results are separate post-publication evidence and are not asserted here.

## Qualification limits and compatibility

- Live qualification used a designated **synthetic qualification repository**, proving factory mechanics (lifecycle, controls, isolation, cancel/reconcile, current-head PR/human action, recovery and install), **not usefulness on real projects**.
- The recorded Docker Desktop/macOS arm64 campaign exercised OMP 18.10.3 protocol 8/8, Claude Code 2.1.296 protocol 8/8 and manual bootstrap 10/10. Both support tiers remain **experimental**. These observations do not qualify all Linux/macOS/engine combinations; a Lima install receipt is not live Lima two-harness workflow proof. See [the support matrix](docs/adapters.md#reference-harness-support-matrix).
- Effective effort remains **UNKNOWN** on both harnesses. Worker-reported `anthropic/claude-sonnet-5-5` is an observation, not trusted model enforcement or an endorsed model.
- **G4 remains deferred/fail-closed.** No G4 passage, Stage 4, hosted/multi-tenant service, unattended authority, scheduled collection, self-development or upgrade is claimed; Stage 4 has no committed date. Stages 1–3 remain the workflow surface.
- Python 3.12+, macOS and Linux; Windows is unsupported because supervision and cancellation rely on POSIX process groups/signals. There is no organization RBAC, browser dashboard or interactive TUI.
- Runtime, optional-extra and development dependencies are unchanged from 0.6.0.

## Installation

```bash
uv tool install enginery==0.7.0
enginery --version
enginery doctor
```

## Links

- [Changelog](CHANGELOG.md)
- [Dependency manifest](docs/DEPENDENCIES.md)
- [Operator guide](docs/operations.md)
- [Release evidence](docs/RELEASE_EVIDENCE.md)
