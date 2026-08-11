# Milestone Reassessments

Evidence-backed log of the mandatory pre-milestone reassessment gate
(`DEVELOPMENT_PLAN.md` §2). Every unstarted milestone from M6 onward
begins here, before detailed design, branch creation, or code changes.
Local planning material — globally gitignored; never referenced from
implementation code or tracked documentation.

## M20 — Add a docs-currency and release-tooling-completeness check to the release gate

**Date:** 2026-07-22
**Baseline commit:** `ecb2c2091ed9ea3c828ff42a5e544d78e1215a1e` (`origin/main` == local `main`, clean worktree, no open PRs)

### Pre-existing gap noted

`DEVELOPMENT_PLAN.md` §2 and `docs/RELEASE_EVIDENCE.md`/`FUTURE_ENHANCEMENTS.md`
reference `.docs/MILESTONE_REASSESSMENTS.md` as the log recording the
M12b and M13b reassessment findings this milestone is named after,
including the exact `full_system_gate.py --stages` gap and its
corrective PR. That file did not exist on disk at the start of this
session (`read` and a full-filesystem `find` both failed to locate it).
This entry creates the file and grounds itself in the actual git
history and merged evidence below rather than in the missing prior
entries, which are not fabricated here. This gap itself is exactly the
kind of process-discipline lapse M20 is not scoped to fix (it only
adds tooling, not a backfill of missing planning records) — noted for
a human, not auto-corrected.

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` §2 (reassessment-gate paragraph, to be edited
  by this milestone), §4 (M20's `none` release-train row), M20's own
  milestone entry (Section I).
- `FUTURE_ENHANCEMENTS.md` §7 (the two named recurring failure
  patterns this milestone closes).
- `scripts/release_gate.py` (current structure: version/changelog/test
  consistency checks, fresh-artifact build, twine check, install
  smoke; no docs-currency check exists yet).
- `scripts/full_system_gate.py`, `tests/system/test_full_system_gate.py`,
  `tests/system/test_run_stage3_gate.py` (existing `tests/system/`
  conventions this milestone's test file must match: import the script
  directly via `pythonpath = [".", "scripts"]`, no reimplementation).
- Git history for the two named incidents, since the log recording
  them was missing:
  - Verification-tooling-completeness gap: commit `571f071`
    (`build(release): support cumulative Stage 1+2 gate in
    full_system_gate.py`, PR #123, `m12b/pre-release-gate-01`) and
    commit `641a9d3` (`test(release): extend full_system_gate.py with
    a restart-capable Stage 3 leg`, PR #133,
    `m13b/pre-release-gate-01`). Both commit messages state the same
    pattern directly: the release-preparation milestone's own required
    verification command (`--stages 1,2` for M12b, `--stages 1,2,3`
    for M13b) was not supported by the script until a corrective PR
    landed first, discovered only after the release-preparation
    milestone had already started. `docs/RELEASE_EVIDENCE.md`'s
    `v0.2.0` and `v0.3.0` sections independently corroborate this:
    each names its own PR #123/#133 as "a pre-release tooling PR (not
    one of the milestone's two named release-preparation PRs)."
  - Docs-currency gap: commit `9d9c7bf` (`docs: sync README and
    operator/adapter/positioning docs to v0.3.0`, PR #136,
    `docs/sync-v0.3.0-status`, merged after `v0.3.0` had already
    published). Diffed against its parent: `README.md` and
    `docs/operations.md` both asserted a specific stale self-version
    (`` `v0.1.0` `` in a "Status" line, an "Enginery is `v0.1.0`"
    sentence, a "`v0.1.0` is published to PyPI" install instruction,
    and a doctor-output code example reading
    `package_metadata: enginery 0.1.0 installed`) after `v0.2.0` and
    `v0.3.0` had both already shipped and published for real.
    `docs/overview.md`, `docs/pitch.md`, and `docs/workflows.md`
    separately described Enginery as an unimplemented "product
    concept" using specific literal phrases (`"Product concept and
    architecture reference"`, `"Enginery is not yet implemented"`,
    `"design targets, not demonstrated product capabilities"`, and
    similar) after Stages 1-3 had shipped for real.
- Full current tracked-doc tree (`git ls-files -- '*.md'`, 21 files)
  grepped for both candidate detection strategies before choosing one,
  to ground the false-positive risk the plan names explicitly.

### Confirmed assumptions

- `scripts/release_gate.py` has no docs-currency check today; wiring
  a new one in is additive, not a rewrite.
- `tests/system/` is the correct location and import convention for
  the new test file (two existing precedents).
- `scripts/` ships as repository tooling only (`pyproject.toml`'s
  `[tool.hatch.build.targets.wheel]` packages `src/enginery` only);
  `none` release-train, no version bump, confirmed by reading
  `pyproject.toml` and `DEVELOPMENT_PLAN.md` §4's M20 row.
- The current canonical version is `0.3.0` (`pyproject.toml`), and the
  live tracked-doc tree is already fully corrected (post-PR #136); a
  correctly designed check must pass against it today with zero
  changes.

### Invalidated assumption (the one that changed the design)

The milestone's own SCOPE line reads: "fails closed if a tracked doc
contains the previous product version number outside a
changelog/evidence file." A literal implementation — grep every
tracked doc for any prior version substring (`v0.1.0`, `v0.2.0`) —
was tested against the real current doc tree first, per this
reassessment's own evidence-gathering step, and rejected: `v0.1.0` and
`v0.2.0` appear correctly and pervasively today in `README.md`,
`RELEASE_NOTES.md`, `docs/operations.md`, `docs/overview.md`,
`docs/pitch.md`, `docs/workflows.md`, and `docs/DEPENDENCIES.md`'s and
`docs/release-readiness-v0.1.0.md`'s own legitimate historical
sections (e.g. "Layered on `v0.1.0`'s Stage 1 and `v0.2.0`'s Stage
2..."), none of which is a changelog or `docs/RELEASE_EVIDENCE.md`.
A blanket substring check would fail closed against the *correct*
current doc set, violating the milestone's own acceptance criterion
("passes against the current, already-corrected doc set") and would
require expanding the two-file exclusion list well beyond what the
plan's own risk paragraph names — exactly the false-positive risk the
plan calls out.

Replan: scope the version check to the specific sentence forms that
actually broke (self-declaration of Enginery's *own* current version —
"Enginery is `vX.Y.Z`", "`vX.Y.Z` is published to PyPI", a doctor-output
`package_metadata: enginery X.Y.Z installed` example, a `` `vX.Y.Z`
(Stage N only) `` status opener), each checked against the canonical
`pyproject.toml` version rather than against a fixed "previous"
literal. This is the exact shape of both real defects, is provably
false-positive-free against the current tree (verified directly), and
degrades gracefully to a documented, easily extended pattern list
(the milestone's own "configurable" requirement) rather than a
brittle single regex. The stale-status-phrase list is similarly scoped
to the exact literal phrases the `9d9c7bf` diff proves were live and
wrong, not a generic "not yet implemented" substring — Stage 4 is
correctly described as "not yet implemented" today, and a generic
phrase would false-positive against that true claim.

This is a detection-strategy refinement within the milestone's own
stated scope and acceptance criteria, not a scope change: it still
"fails closed against a real stale-doc fixture" and "passes against
the current doc set," per the FINAL VERDICT gate.

### Newly discovered risks

- A configurable phrase/pattern list is only as good as its coverage;
  a future doc could go stale in a new sentence shape this list does
  not recognize. Documented in the script's own module docstring as
  an explicit maintenance note (extend the list when a new
  self-declaration or status-framing convention appears), matching
  this repository's existing "no speculative generalization" norm —
  covering the two proven incidents now, not hypothesizing every
  future phrasing.
- `git ls-files` requires a real git repository; the check assumes it
  runs inside one (true for `scripts/release_gate.py` invocation and
  CI). No fallback is needed — `release_gate.py` itself has the same
  implicit assumption (it already shells out to `uv`/`uvx`/`git`-adjacent
  tooling without a non-git fallback).

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

None beyond the detection-strategy refinement above. Objective, scope,
deliverables, acceptance, verification commands, 2-PR stack topology,
and rollback all remain exactly as written in `DEVELOPMENT_PLAN.md`
M20 and `EXECUTION_PROMPTS.md`'s M20 `/goal` block.

### Result

**GO.**

### Human approval reference

Recorded via the `ask` tool in this session before branch creation or
code changes, per `DEVELOPMENT_PLAN.md` §2: "every assessment outcome,
including GO, requires recorded explicit human approval... before
detailed design, branch creation, or code changes."

## M18 — Report gate G4 readiness against the registered conditions

**Date:** 2026-07-22
**Baseline commit:** `233e416bbe59d899adf78dc5118c9ab6fcca2f5b` (`origin/main` == local `main`, clean worktree, no open PRs, M20 merged)

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` §2 (GAP note on gate G4's two structurally-failing
  conditions and the reassessment-gate paragraph), §4 (M18's
  `> GAP: unresolved` release-train row), §8 (the six G4 conditions,
  verbatim), M18's own milestone entry (Section I).
- `FUTURE_ENHANCEMENTS.md` §3 (the G4 condition table and its "current
  state" column, matching §8 word for word).
- `docs/design.md` §13 (evaluation/metrics scorecard) and its Stage 4
  gate-deferred paragraph (data-threshold entry gate wording).
- `src/enginery/evaluation/outcomes.py` (`OutcomeCaptureService`,
  `CompletenessReport`, `compute_completeness` — confirmed the
  completeness derivation is versioned but *not* time-windowed today;
  `reference_time` is accepted and explicitly documented as "reserved
  for a future time-windowed derivation version").
- `src/enginery/evaluation/queries.py` (`list_interventions`,
  `list_failures` — both scoped to one `run_id`; no ledger-wide
  variant exists yet; `aggregate_type` is deliberately caller-supplied
  so `evaluation` never imports `engine`).
- `src/enginery/cli/outcome.py`, `src/enginery/cli/main.py` (CLI
  subcommand/argparse wiring conventions, exit-code conventions via
  `enginery.cli._exit_codes`).
- `scripts/check_import_boundaries.py` (`LAYER_ALLOWED_IMPORTS`):
  `evaluation` may import only `domain`, `application`, `evaluation`,
  `ledger` — not `engine` or `workflows`. `cli` may import all of
  those plus `engine`, `workflows`, `adapters`. This is enforced by
  `tests/unit/test_import_boundaries.py` on every `pytest -q` run, not
  just a documentation convention.
- `config/performance-bounds.toml` and `scripts/performance_baseline.py`
  (the existing precedent for a human-maintained, versioned "floor"
  file the plan's SCOPE line names as analogous).
- `src/enginery/domain/enums.py` (`WorkKind`, `RiskClass` — the closed
  "workflow type" and "risk class" vocabularies G4's first condition
  references), `src/enginery/domain/principal.py` and
  `src/enginery/policy/approval.py` (`AuthorityPrincipal`,
  `ApprovalRegistry.register_human` — confirmed this registry is an
  in-memory, per-process construction seeded from a hardcoded fixture
  list in every current caller, including `scripts/full_system_gate.py`
  and `scripts/run_stage3_gate.py`; there is no durable, ledger-backed
  roster of "registered" human principals anywhere in this codebase
  today).
- `src/enginery/domain/run.py`, `src/enginery/domain/work_item.py`
  (`Run.repository`, `WorkItem.work_kind`, `WorkItem.risk_class`,
  `WorkItem.repository_targets`).
- `src/enginery/engine/runtime.py` (`RUN_AGGREGATE_TYPE`,
  `RUNTIME_NODE_AGGREGATE_TYPE`, `CoordinatorRuntime.register_run` —
  confirmed the `"run"` aggregate is written exactly once
  (`run.created`) and never updated again for any CLI-reachable path;
  `RunState.SUCCEEDED` is declared in the transition table but no code
  anywhere persists a run transitioning to it — Stage 1's merge-ready
  outcome is a node-level projection, not a `Run.state` mutation).
- `src/enginery/workflows/stage1.py` (`Stage1RunRequest.initial_state`,
  `stage1_request_from_state`, `verify_merge_ready` — confirmed the
  `"run"` aggregate's payload embeds the full bound `WorkItem`, and the
  `"{run_id}:verify"` runtime-node's `status` field, already used
  internally by `runtime.py`'s own PR-publication gate
  (`projection.state.get("status") != "passed"`), is the actual durable
  "this run reached its terminal merge-ready evidence" signal — not
  `Run.state`).
- `src/enginery/incidents/service.py` (`WORK_ITEM_AGGREGATE_TYPE`):
  confirmed a standalone top-level `"work_item"` ledger aggregate is
  written only by `IncidentService` (Stage 3); Stage 1 never writes one
  (its `WorkItem` lives only inside the embedded `"run"` payload above).
  Grepped `RUN_AGGREGATE_TYPE`/`RunState` usage across
  `src/enginery/plans` and `src/enginery/incidents`: neither Stage 2
  (plans/stacks) nor Stage 3 (incidents) registers a `Run` aggregate at
  all — only Stage 1 does.
- `git log`, `git branch -a`: confirmed a clean `main`, no stale local
  or remote branches from a prior M18 attempt, no existing `tests/gate`
  directory, `src/enginery/cli/gate.py`, or `config/gate-g4-floor.toml`.
- No live local dogfood ledger (`.db` file) exists anywhere in this
  workstation checkout outside `tests/fixtures/ledger.db` (a test
  fixture) and unrelated `.mypy_cache`/`.archex` SQLite caches
  (confirmed via `find . -iname '*.db'`). The FINAL VERDICT's "real
  local ledger" run below therefore uses a freshly initialized, empty
  ledger — the only "real local ledger" this workstation actually has.

### Confirmed assumptions

- M14a is externally merged and shipped in `v0.1.0` (per M14a's own
  Section G entry and `DEVELOPMENT_PLAN.md` §5's target-release table);
  its `OutcomeCaptureService.completeness()` is safe to reuse directly.
- `> GAP: unresolved` remains the correct release-train framing (§4's
  M18/M19 row); no human has since assigned a release train. No
  version, changelog, or publication work belongs in this milestone.
- The six G4 conditions in `DEVELOPMENT_PLAN.md` §8 are unchanged since
  the plan revision that added M18 (`FUTURE_ENHANCEMENTS.md` §3's table
  matches §8 verbatim); no replan is needed on that front.
- `tests/gate` does not exist yet; this is a new test-directory family,
  matching the plan's own `Verification` field
  (`uv run pytest tests/gate -q`).

### Invalidated or refined assumption (the one that changed the design)

The plan's SCOPE line reads "a repository-diversity count derived from
configured repository targets already recorded in the ledger," which
reads most literally as `WorkItem.repository_targets`. Investigation
found that `WorkItem` is not a standalone, ledger-wide-enumerable
aggregate for Stage 1 runs (the only workflow that registers a `Run`
today) -- it is embedded inside each Stage 1 run's own `"run"`
aggregate payload. `Run.repository` (the single, actually-exercised
repository a run bound to) is available on that same embedded payload,
is `Run`-scoped exactly as G4's own condition text asks ("runs from at
least two repositories"), and requires no separate join. Replan:
compute repository diversity from `Run.repository` across every
registered Stage 1 run, not `WorkItem.repository_targets` (which lists
*candidate* targets declared at intake, not repositories a run actually
bound to). This is a data-source refinement within the same stated
scope and acceptance criteria ("a repository-diversity count derived
from ... the ledger") -- it does not change what the report measures or
widen/narrow any acceptance criterion.

A second, related refinement: G4's first condition ("completed runs
across at least two workflow types and at least two risk classes") can,
today, only be measured from Stage 1 run data -- Stage 2 (plans/stacks)
and Stage 3 (incidents) do not register a `Run` aggregate at all in the
current implementation, so `WorkKind`/`RiskClass` breadth is
structurally capped at whatever Stage 1 alone has produced
(`WorkKind.ISSUE` only, today). This is not a defect in this
milestone's design -- M18 is explicitly scoped as read-only reporting
over "already-captured M14a outcome/intervention/completeness data,"
and M14a's own outcome-capture pipeline is itself Stage-1-scoped by
design (`register_pending` calls exist only in `stage1.py`). Building
cross-stage `Run`/`WorkItem` capture for Stage 2/3 would be new capture
infrastructure outside this milestone's SCOPE line, not a reporting
gap this instrument should paper over. The report will therefore
accurately show workflow-type breadth capped at one until a future,
separately-scoped milestone extends durable run capture to Stage 2/3 --
recorded as a newly discovered risk below, not treated as a defect to
silently work around.

### Newly discovered risks

- Stage 2/3 do not register `Run` aggregates, so `completed_run_count`,
  workflow-type breadth, and risk-class breadth measured by this
  command are Stage-1-only today, understating whatever real diversity
  might exist once Stage 2/3 dogfooding accumulates. This is disclosed
  in the command's own per-condition `detail` text (which states the
  measured breadth plainly) rather than hidden; it does not change the
  fail-closed contract (an unset floor still reports `unmeasured`, and
  a set floor is evaluated honestly against whatever Stage-1-only data
  exists).
- No durable, ledger-backed roster of "registered" `AuthorityPrincipal`
  humans exists anywhere in this codebase; `ApprovalRegistry` is always
  constructed fresh, in-process, from a hardcoded fixture list. The
  registered-principal count this milestone reports therefore comes
  from a new, purely human-maintained roster inside the registered-floor
  configuration file itself (edited by hand, exactly like
  `config/performance-bounds.toml`) rather than from any existing
  registration mechanism -- consistent with "this command only reads
  existing state" and "do not implement any action that ... registers a
  human principal," since editing a checked-in TOML file by hand is not
  a registration *action* this command performs.
- The outcome-capture completeness derivation
  (`compute_completeness`/`CompletenessReport`) has no actual
  trailing-window implementation yet (`reference_time` is accepted but
  unused, "reserved for a future time-windowed derivation version").
  G4's condition text says "over the trailing window." This milestone
  will report completeness using the only durable derivation that
  exists -- the current all-time captured/indeterminate ratio -- and
  will say so explicitly in the condition's `detail` text so the report
  never implies a rolling-window computation that does not exist. This
  is a measurement-fidelity gap worth a human's attention at the next
  quarterly review, not a blocker for this milestone (SCOPE explicitly
  calls for reusing M14a's *existing* projection, not extending it to
  add windowing).
- G4's corpus-diversity condition text also allows "at least one
  external adopter" as an alternative to two repositories. No adapter
  or signal for external-adopter status exists anywhere in this
  codebase (this repository's own design assumes one ledger per local
  installation, so an external adopter's usage would live in a
  different, unreachable ledger entirely). This command measures only
  the repository-count branch and treats external-adopter status as
  untracked/absent, which is the only way to report the required `fail`
  state honestly today without fabricating an adopter signal that does
  not exist.

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

None beyond the two data-source refinements above (repository diversity
from `Run.repository` rather than `WorkItem.repository_targets`;
completed-run breadth necessarily Stage-1-scoped today). Objective,
scope, deliverables, acceptance, verification commands, 3-PR stack
topology, and rollback all remain exactly as written in
`DEVELOPMENT_PLAN.md` M18 and `EXECUTION_PROMPTS.md`'s M18 `/goal`
block.

### Result

**GO.**

### Human approval reference

Recorded via the `ask` tool in this session before branch creation or
code changes, per `DEVELOPMENT_PLAN.md` §2: "every assessment outcome,
including GO, requires recorded explicit human approval... before
detailed design, branch creation, or code changes."

## M19 — Close the pilot-identified Stage 1/2/3 operator-experience gaps

**Date:** 2026-07-22
**Baseline commit:** `dc8daa828c8afd28fb2aaa0f766c858bea723a7c` (`origin/main` == local `main`, clean worktree, no open PRs, M18/M20 merged)

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` §2 (M18-M23 backlog/release-train GAP notes,
  reassessment-gate paragraph), §4 (M19's `> GAP: unresolved` row),
  M19's own milestone entry (Section I, full In/Out-of-scope line read
  unwrapped since the summarized `read` truncated it).
- `FUTURE_ENHANCEMENTS.md` §5 (Tier B operator-experience gaps —
  confirms this is a promoted-to-M19 pointer, not independent scope).
- `docs/pitch.md` "Operator burden" pilot finding and "Decision"
  paragraph (both read unwrapped) — the three named gaps verbatim: (1)
  no `stage1` command constructs a run request; (2) no command
  releases or inspects a stuck workspace reservation; (3) a node that
  reaches `queued` but is not selected within its registering tick has
  no automatic retry path, only an explicit `stage1 cancel`. Confirmed
  none of these produced an unsafe/duplicated external effect during
  the real pilot, and the operator's conditional acceptance names
  closing exactly these three gaps.
- `docs/operations.md` full command surface, "Running a Stage 1
  workflow," and "Recovery semantics" sections — confirms today's
  actual CLI has no request-builder, no workspace inspect/release
  command, and `adapter doctor`'s sample output already documents
  `local-publication`/`local-deployment-fixture` (the fixture-only
  providers `local_provider_statuses()` already reports) — the real,
  non-fixture Stage 2/3 broker adapters are not covered anywhere.
- `src/enginery/cli/main.py`, `src/enginery/cli/doctor.py`,
  `src/enginery/cli/stage1.py` (full raw reads) — current subcommand
  wiring conventions, exit-code mapping via `EngineryError` bubbling
  to `main()`, and confirmation `_run_adapter_doctor` today
  unconditionally asserts every `AdapterStatus` carries a fingerprint
  (would crash once a genuinely unavailable/misconfigured real adapter
  status is added — a discrete design point below).
- `src/enginery/workflows/stage1.py` (`Stage1RunRequest`,
  `stage1_request_from_state`/`_run_from_state` and every private
  decoder) — the exact JSON shape `stage1 start --request` requires;
  confirmed via `Stage1RunRequest.initial_state()`/`.digest` that a
  request-builder need only construct the same dataclasses real
  fixtures already construct (`scripts/full_system_gate.py`,
  `tests/cli/test_stage1.py::_request`,
  `tests/workflows/test_stage1_runtime.py`) and serialize with
  `initial_state()` — no new decode path needed.
- `src/enginery/domain/work_item.py`, `src/enginery/domain/run.py`,
  `src/enginery/domain/digests.py`, `src/enginery/workflows/issue_to_pr.py`
  (`issue_to_pr_manifest()`, the only implemented Stage 1 manifest) —
  every field a request-builder must populate and its validation.
- `src/enginery/capabilities/lock.py` (`CapabilityLock.digest()`),
  `src/enginery/cli/capability.py` (`check_lock`,
  `enginery.capabilities.serialization.read_lock`) — confirmed a real,
  reusable way to derive `Run.capability_lock_digest` from the same
  on-disk lockfile `enginery capability lock` already reads, instead
  of requiring the operator to hand-compute a digest.
- Grepped the full repository for `environment_manifest_digest` /
  `configuration_snapshot_digest`: no computation exists anywhere
  outside `Run`'s own field declaration and its
  serialization/deserialization. These remain genuinely unimplemented,
  reserved concepts (matching `DEVELOPMENT_PLAN.md` §2's own
  "environment manifest"/"configuration snapshot" vocabulary for later
  milestones) — a discrete design point below.
- `src/enginery/engine/runtime.py` (`CoordinatorRuntime.release_workspace`,
  full method body) — the exact fenced-proof discipline: reads the
  process-manager-backed reservation by `repository_id`, requires
  `reservation.run_id == run_id`, requires `reservation.status ==
  "retained"` (refusing any other status, including an actively
  materialized/live-leased workspace), and performs the release
  through `self._workspaces.cleanup(reservation, epoch=epoch, now=now)`
  under the coordinator's epoch fencing token. The new CLI command
  must call this method directly and let its `ExternalConflictError`/
  `InternalInvariantViolationError` propagate unmodified — confirmed
  `main()`'s existing `except EngineryError` handler already maps
  `failure_class` to the correct exit code, so no CLI-level
  pre-check is needed or wanted.
- `src/enginery/engine/workspace.py` (`GitWorktreeBackend`,
  `WorkspaceReservation`, `_workspace_event`, `_reservation_state`,
  `_reservation_from_state`) — reservations are stored twice per write
  (a `workspace-reservations` process-manager state keyed by
  `repository_id`, used by `read_reservation`/`release_workspace`, and
  a mirrored `"workspace"` event-sourced projection with the identical
  payload). `LedgerService.list_projections(aggregate_type="workspace")`
  already enumerates every repository's current reservation state with
  no new ledger machinery — the natural, reuse-only backing for a
  listing command. No existing method lists every process-manager
  state by name, so the inspect command lists via the projection
  aggregate and releases via `read_reservation`/`release_workspace`
  (already the release call's own read path) — both draw from
  ledger-durable state written atomically together.
- `src/enginery/engine/scheduler.py` (`ReadinessScheduler.plan`,
  `SchedulingLimits`) and `src/enginery/engine/runtime.py`
  (`CoordinatorRuntime.tick`, `_register`, `_requests_from_ledger`) —
  confirmed `available_slots = limits.global_concurrency - len(active)`
  with `active` computed from every `runtime_node` projection in the
  *entire* ledger (not scoped to one run), so one run's already-running
  node can starve another run's newly registered node on its very
  first tick.
- `src/enginery/workflows/stage1.py`
  (`Stage1RunService.dispatch_implementation`, `.next_action`,
  `.advance`) — traced the exact mechanism behind the pilot's third
  finding: `dispatch_implementation` registers the `implement` node
  (durably "queued") and ticks in the same call; if the scheduler does
  not select it, `tick.dispatched` is empty and
  `dispatch_implementation` raises `ExternalConflictError("qualified
  implementation was not scheduled", ...)` — but the registration
  already committed. On every subsequent call, `next_action` sees
  `implement_state is not None`, status `"queued"` (`!= "passed"`),
  no result file on disk, and returns `Stage1ProgressionAction.WAIT`
  forever; `advance()`'s action dispatch table has no handler for
  `WAIT`, so it never re-ticks this node. This durably confirms the
  pilot's exact wording ("no automatic retry path") as a genuine,
  reproducible property of the shipped code, not pilot-run noise.
  `CoordinatorRuntime.cancel_node`'s `elif status != "queued":
  raise ...` branch falls through for a `"queued"` node (no active
  lease required), matching `stage1 cancel`'s already-documented
  recovery path exactly.
- `src/enginery/application/delivery_ports.py` (`ReleasePort`,
  `DeploymentPort` protocols, both declaring `probe() -> AdapterStatus`),
  `src/enginery/application/adapter_types.py` (`AdapterStatus`,
  `AdapterAvailability` — including `MISCONFIGURED`, unused anywhere
  today), `src/enginery/adapters/local.py` (`local_provider_statuses`
  — today only the eight deterministic *fixture* providers, never the
  real Stage 2/3 adapters), `src/enginery/adapters/github.py`
  (`GitHubReleaseAdapter.probe()` — checks only `gh --version`, never
  touches `repository`/`credential_reference`), `src/enginery/adapters/pypi.py`
  (`PyPiAdapter.probe()` — checks only `uv --version`, never touches
  `project_name`/`index_url`/`publish_url`/`json_api_base`),
  `src/enginery/adapters/local_service.py`
  (`LocalServiceDeploymentAdapter.probe()` — today unconditionally
  returns `AVAILABLE` with no check of `app_script`/`python_executable`
  at all; genuinely the "configuration sanity" gap M19 names). None of
  the three probes performs network I/O.
- `src/enginery/workflows/plan_to_release.py` (`Stage2ReleaseWorkflow`)
  and `src/enginery/engine/release_manifest.py`
  (`VersionChangelogBroker`) — confirmed the plan's "Stage 2 release
  broker" phrase maps to the real `GitHubReleaseAdapter`/`PyPiAdapter`
  pair `Stage2ReleaseWorkflow` composes, not `VersionChangelogBroker`
  (a pure local file-writer with no `probe()`/network surface and
  nothing to report).
- Confirmed via `importlib.metadata.metadata("enginery")` in this
  session that an installed (non-editable) `enginery` distribution
  exposes `Project-URL: Repository, https://github.com/Mathews-Tom/Enginery`
  and `Name: enginery` — a real, no-network, works-after-a-clean-install
  source for the GitHub repository and PyPI project identity a
  doctor-time `GitHubReleaseAdapter`/`PyPiAdapter` probe can bind to,
  in preference to reading `pyproject.toml` (not shipped in the wheel,
  unavailable outside a source checkout).
- `scripts/check_import_boundaries.py` (`LAYER_ALLOWED_IMPORTS`):
  `cli` may import `domain`, `application`, `evaluation`, `ledger`,
  `engine`, `workflows`, `adapters` — every module this milestone's
  four PRs need to import from `cli` is already permitted.
- `tests/cli/test_doctor.py`,
  `tests/engine/test_runtime.py`/`test_coordinator_runtime.py`,
  `tests/workflows/test_stage1_runtime.py` (`RecordingWorkLedger`,
  `TerminalWorkLedger`, `_request`/`_snapshot` fixture-construction
  patterns; `test_stage1_dispatch_implementation_self_determines_repair_or_rejects_exhaustion`
  and the qualify-then-dispatch test around line 660) — established,
  reusable fixture-construction conventions for every new test this
  milestone adds, so no new test double vocabulary is invented.

### Confirmed assumptions

- M8, M12, and M13 are externally merged into `main` (git log shows
  their merge commits well before the current `HEAD`, and `v0.3.0` is
  already released per `docs/RELEASE_EVIDENCE.md`).
- `> GAP: unresolved` remains the correct release-train framing (§4's
  M18/M19 row); no human has since assigned a release train. No
  version, changelog, or publication work belongs in this milestone.
- The three named gaps in `docs/pitch.md` are still open exactly as
  described: grepped the full CLI surface (`main.py`) and confirmed no
  `stage1 build-request`/`stage1 request` command, no `workspace`
  command family, and no Stage 2/3 broker entries in
  `local_provider_statuses()` exist today.
- `docs/design.md` §§8/11-13/15/17 (Stage 1/2/3 workflow/evidence
  contracts) are unaffected by this milestone's scope: every new
  command reuses existing engine/workflow entry points
  (`CoordinatorRuntime.release_workspace`,
  `stage1_request_from_state`, `Stage1RunRequest`) without introducing
  a new workflow node, policy rule, or evidence field.

### Newly discovered risks

- `_run_adapter_doctor` today unconditionally asserts every
  `AdapterStatus.fingerprint is not None` and always returns `SUCCESS`.
  Adding real (non-fixture) broker probes that can legitimately be
  `UNAVAILABLE`/`MISCONFIGURED` (fingerprint `None`) will crash that
  assertion unless `_run_adapter_doctor` is rewritten to branch on
  fingerprint presence and compute its exit code from every status's
  availability, not a hardcoded `SUCCESS`. This is a necessary, minimal
  change to `_run_adapter_doctor` itself (not the workflow/policy/
  evidence contracts the GLOBAL CONSTRAINTS protect) and is now folded
  into PR4's scope.
- `environment_manifest_digest` and `configuration_snapshot_digest`
  have no real computation anywhere in this codebase today. The
  request-builder cannot derive them without fabricating a mechanism
  this milestone does not own building. Resolution: expose them as
  explicit builder inputs (`ALGORITHM:HEX` digest strings or a file
  path to hash via `Digest.of_bytes`), never a silently invented
  default — the operator supplies real bytes or a real digest; the
  tool never guesses. This is a design refinement within "guided or
  flag-driven," not a scope change.
- `capability_lock_digest` DOES have a real, reusable source
  (`enginery.capabilities.serialization.read_lock(...).digest()`
  against the same lockfile `enginery capability lock` already reads,
  defaulting to `.enginery/capabilities.lock.json`, falling back to
  `CapabilityLock(entries=()).digest()` when no lockfile exists — "the
  engine works with Armory disabled" is already an accepted state per
  `capabilities/lock.py`/`cli/capability.py`). The builder derives this
  automatically instead of requiring a hand-computed digest, directly
  reducing the pilot's reported manual-construction friction.
- The fault-injection test for the third gap needs a way to
  deterministically starve global scheduling capacity. Confirmed the
  simplest reproduction is a plain, unrelated `FixtureDispatch` from a
  different run/repository ticked to `"running"` first (consuming the
  sole `global_concurrency=1` slot the CLI always uses), then calling
  `Stage1RunService.dispatch_implementation` for a real, qualified
  Stage 1 request — no live OMP/GitHub credentials or subprocess
  execution needed, since a rejected (non-selected) node is never
  dispatched to a subprocess at all.
- `LocalServiceDeploymentAdapter.probe()` is never called from any
  workflow code path today (only `deploy`/`rollback`/`observe` are
  exercised by `scripts/fault_inject_deployment.py`,
  `full_system_gate.py`, `run_stage3_gate.py`) — tightening it from an
  unconditional `AVAILABLE` to a real, read-only sanity check
  (`app_script.is_file()`, `python_executable` resolvable) cannot
  change Stage 3's deploy/rollback/observe workflow behavior, policy,
  or evidence contract, since nothing currently reads its return value
  outside a doctor-style caller this milestone is adding.

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

None. The four refinements above (derive `capability_lock_digest` from
the real lockfile instead of a hand-typed value; require explicit,
non-fabricated `environment_manifest_digest`/`configuration_snapshot_digest`
inputs; rewrite `_run_adapter_doctor`'s fingerprint-handling and exit
code so it does not crash on a real non-fixture probe result; harden
`LocalServiceDeploymentAdapter.probe()` itself rather than
reimplementing an equivalent check in the CLI) are implementation
choices inside the milestone's own stated deliverables and acceptance
language ("guided or flag-driven," "identical fenced-proof discipline
... reused, not reimplemented," "extend `doctor`/`adapter doctor`
... without a live network call"). Objective, scope, deliverables,
acceptance, verification commands, the four-PR stack topology, and
rollback all remain exactly as written in `DEVELOPMENT_PLAN.md` M19
and `EXECUTION_PROMPTS.md`'s M19 `/goal` block.

### Result

**GO.**

### Human approval reference

Recorded via the `ask` tool in this session before branch creation or
code changes, per `DEVELOPMENT_PLAN.md` §2: "every assessment outcome,
including GO, requires recorded explicit human approval... before
detailed design, branch creation, or code changes."

## M23 — Hands-on competitive capability matrix

**Date:** 2026-07-22
**Baseline commit:** `d92cdbda0100bdc3672d791db25acd3f3ec0efee` (`origin/main` == local `main`, clean worktree, no open PRs, M19 merged)

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` M23 (Section I) and §2 (mandatory reassessment
  gate paragraph).
- `FUTURE_ENHANCEMENTS.md` §8 (names OpenHands' Agent Control Plane,
  Databricks' Omnigent, and Guild.ai as the candidate entrants).
- `docs/overview.md` §6 ("Landscape" table distinguishing "Coding-agent
  workers" from "Agent frameworks" from a dedicated control-plane
  category) and §7 ("Differentiation evidence required": ambiguous
  side effects, exact-head CI/evidence binding, approval supersession
  after input changes, provider-neutral recovery).
- `docs/design.md` §§3, 7-9, 14 (the exact mechanism definitions this
  milestone must test against): the four-outcome reconciliation
  protocol (`not_found` / `found_matching` / `found_conflicting` /
  `indeterminate`, hard rule 7 "an ambiguous side effect must
  reconcile before retry"), the stale-evidence hard rules (hard rule
  6 "stale work, base, head, artifact, or evidence subjects cannot
  satisfy a terminal contract"; "a CI result is valid only for the
  exact commit bound to the evidence contract"), and the
  approval-supersession rule ("any change to a bound input supersedes
  the approval").
- `scripts/adversarial_merge_ready_gate.py` and
  `scripts/adversarial_policy_gate.py` (the actual adversarial
  fixtures this milestone's scenarios are drawn from: stale-CI,
  self-waiver, changed-head, duplicate-check fixtures for stale
  evidence; hard-rule-bypass and self-approval fixtures for approval
  supersession).
- One pre-existing gap noted independently of this milestone's own
  scope: `EXECUTION_PROMPTS.md`'s M23 `/goal` block and
  `DEVELOPMENT_PLAN.md` M23 both cite `.docs/strategy.md` §5 ("Claim
  discipline") as a source of truth. `read` and a full `.docs/`
  directory listing both confirm no `strategy.md` file exists in
  `.docs/` today (only `BACKLOG_SEQUENCE.md`, `DEVELOPMENT_PLAN.md`,
  `FUTURE_ENHANCEMENTS.md`, `EXECUTION_PROMPTS.md`,
  `MILESTONE_REASSESSMENTS.md`, `handoff.md`). This mirrors the exact
  shape of the gap M20's own reassessment entry recorded for a missing
  `MILESTONE_REASSESSMENTS.md` reference: a planning document is cited
  by section number from another planning document but was never
  checked in, or was renamed/consolidated, before this milestone was
  authored. `docs/overview.md` §7 independently states the same claim
  -discipline rule this milestone needs ("Any public claim that a
  specific mechanism is unique to Enginery additionally requires
  hands-on verification of the closest control-plane entrants;
  secondary-source absence is not evidence of absence" and "Before
  claiming a mechanism is unique or a competitor gap is material,
  Enginery must test the closest entrants against the same
  scenarios..."), so this milestone proceeds grounded in
  `docs/overview.md` §7 (a tracked, published document) instead of the
  missing `.docs/strategy.md` §5. Not auto-corrected — noted for a
  human, matching M20's precedent of disclosure over silent
  workaround.

### Confirmed assumptions

- M23 has no milestone dependency (`DEVELOPMENT_PLAN.md` §3, §4's
  `none` release-train row for M23) and is independent of the
  `v0.1.0`-`v0.3.0` trains; nothing needed to externally merge first
  beyond the already-merged M19.
- The three ambiguous-side-effect / stale-evidence /
  approval-supersession scenarios are drawn directly from named,
  already-implemented hard rules and adversarial fixtures (above), not
  invented for this milestone.
- `RELEASE PREP: not-required` and `RELEASE TARGET: none` remain
  correct; `scripts/release_gate.py` is not invoked by this milestone.

### Re-verification of active/comparable entrants (required before starting)

Live web research (2026-07-22) confirms all three FUTURE_ENHANCEMENTS.md
§8 entrants are still active, and each was reached directly (not solely
via marketing copy):

- **OpenHands Agent Control Plane** — still active. The OpenHands
  project itself has restructured since the plan was authored: the
  top-level `OpenHands/OpenHands` repository now hosts "Agent Canvas"
  (a self-hosted developer control center), while the actual agent
  runtime/SDK moved to the separate `OpenHands/software-agent-sdk`
  repository (MIT license, 923 stars at fetch time). Both are real,
  currently maintained, publicly cloneable open-source repositories
  confirmed via direct GitHub API fetch, not a vendor claim.
- **Databricks' Omnigent** — still active, and more directly
  reproducible than the plan anticipated: `omnigent.ai` and
  `github.com/omnigent-ai/omnigent` (Apache-2.0, 7616 stars at fetch
  time, alpha status) confirm it is a real, self-hostable,
  one-command-install (`curl ... install_oss.sh | sh`) open-source
  project, footer-credited to "the Databricks AI team and Neon" —
  consistent with `docs/overview.md` §6's "Databricks open-sourced
  Omnigent" framing, though the product is branded and hosted
  independently of databricks.com (`https://www.databricks.com/product/omnigent`
  returns HTTP 404; the Databricks-hosted "preview" surface referenced
  by some secondary sources is a separate, workspace-gated feature,
  not the open-source project used for this milestone's reproduction).
- **Guild.ai** — still active. `guild.ai` and `docs.guild.ai` are live;
  Guild raised a combined seed+Series A ($44M, led by GV) in March 2026
  per Axios/PitchBook/SiliconAngle coverage, and its own docs describe
  a real, currently-shipping product with a self-serve `app.guild.ai`
  sign-in (Google/GitHub OAuth or emailed magic link) distinct from the
  marketing site's "Book a demo" call-to-action.

No fourth entrant met the bar for "closest control-plane entrant" that
`docs/overview.md` §6 itself sets: a dedicated control plane for
coding/engineering-agent governance, not a general agent-orchestration
*framework* (LangGraph, CrewAI, AutoGen, Semantic Kernel — build
agents, do not govern a fleet of already-built agents in production)
and not a generic low/no-code automation platform (n8n, Zapier, Make,
Vertex AI Agent Builder, AWS Bedrock AgentCore — cloud-native workflow
builders without a coding-agent-specific governance surface). This
distinction is `docs/overview.md` §6's own landscape table, not an
invented filter. The three named entrants remain the complete and
correct set for this milestone.

### Invalidated or refined assumption (the one that changed the design)

The plan's REQUIRED WORK line reads "Attempt direct reproduction
against each entrant for each scenario before marking any row 'not
independently verified'." Investigation found that "direct
reproduction" cannot mean the same thing uniformly across three
structurally different entrants:

- OpenHands' Agent Canvas / software-agent-sdk and Omnigent are both
  genuinely open-source and self-hostable with no account gate at all
  (`pip`/`uv`/`npm` install, MIT and Apache-2.0 respectively). For
  these two, "direct reproduction" is interpreted as installing and
  directly inspecting the real, running, currently-shipping
  implementation's source for the exact mechanism each scenario tests
  (idempotent external-operation reconciliation, exact-commit CI/
  evidence binding, approval-digest invalidation on changed diff) --
  confirmed present or absent by reading and cross-referencing the
  actual code paths (`omnigent/policies/builtins/github.py`'s
  ALLOW/ASK/DENY repo-and-branch access gate;
  `openhands-sdk/openhands/sdk/security/{risk,confirmation_policy}.py`'s
  per-action `SecurityRisk` threshold gate), not a full multi-hour
  live agentic run against a real GitHub repository requiring
  provisioned LLM credentials this session does not have standing
  authorization to spend.
- Guild.ai's platform requires signing in at `app.guild.ai` with a
  real Google/GitHub identity or an emailed magic link -- there is no
  anonymous or credential-less self-serve path. Creating an account
  bound to a real human identity on a third-party commercial SaaS
  product is a decision with account/identity consequences for the
  user this session was not given explicit authorization to make, so
  this milestone does not create a Guild.ai account. Guild's own
  current technical documentation (`docs.guild.ai`, fetched directly,
  not a marketing summary) is used instead: the GitHub integration
  reference (operation-by-operation REST pass-through), the
  credential-policies reference (static per-operation-name ALLOW/DENY
  rules), the agent-versions reference (agent-code version rollback),
  and the audit-logs reference (administrative-action log, not
  workflow-evidence log) are each precise enough on their own terms to
  answer whether the platform documents the specific mechanism each
  scenario tests, without requiring a live session.

Replan: the capability matrix records, per row, which of these three
evidence classes was actually used -- "installed and inspected the
entrant's real running open-source implementation," "read the
entrant's own current technical documentation without a live account,"
or "not independently verified, secondary source cited" -- rather than
treating all citations as interchangeable "direct reproduction." This
is a citation-taxonomy refinement within the milestone's own stated
acceptance criterion ("every claim is either a first-hand observation
... or explicitly marked unverified with its secondary source"): a
first-hand reading of an entrant's real, current, non-marketing
primary source (its own shipped code or its own current docs) is
still a first-hand observation, distinct from and stronger than "not
independently verified" against a secondary summary. It does not
relax the requirement that live account creation be attempted before
falling back -- it documents, per entrant, exactly why a live account
was or was not created, so a human reviewer can judge whether that
judgment call was an honest fallback (this reassessment's position)
or a shortcut.

### Newly discovered risks

- None of the three entrants implements, or documents implementing,
  the specific mechanisms these three scenarios test (deterministic
  four-outcome external-operation reconciliation; CI/evidence binding
  to the exact current commit for a terminal claim; approval-digest
  invalidation on a changed diff/base). This is a genuinely
  significant finding but also a risk this milestone's own
  `docs/overview.md` §7 anticipates and forbids overclaiming from: "No
  entry states or implies a mechanism gap without attempting direct
  reproduction" and "secondary-source absence is not evidence of
  absence." Because reproduction was attempted (source inspection for
  the two open-source entrants; current-docs review for Guild.ai) and
  produced a negative result for the specific mechanism, not merely an
  absence of a marketing claim, the matrix records this as an observed
  absence with primary-source citation, not an inferred gap -- and the
  matrix's own prose explicitly states the boundary (these entrants
  govern agent execution/session/cost/access at a different layer than
  Enginery's SCM-workflow terminal-evidence contracts) so a reader
  cannot mistake "this mechanism was not found" for "this product is
  worse."
- Guild.ai rows citing docs-only evidence (no live account) are weaker
  than the two open-source rows citing direct code inspection. The
  matrix marks this distinction explicitly per row rather than
  presenting all three entrants at uniform evidence strength.

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

None beyond the citation-taxonomy refinement above and substituting
`docs/overview.md` §7 for the missing `.docs/strategy.md` §5 as the
tracked-document claim-discipline source. Objective, scope,
deliverables, acceptance, verification commands, 1-PR stack topology,
human review gate, and rollback all remain exactly as written in
`DEVELOPMENT_PLAN.md` M23 and `EXECUTION_PROMPTS.md`'s M23 `/goal`
block.

### Result

**GO.**

### Human approval reference

Recorded via the `ask` tool in this session before branch creation or
code changes, per `DEVELOPMENT_PLAN.md` §2: "every assessment outcome,
including GO, requires recorded explicit human approval... before
detailed design, branch creation, or code changes."

## M21 — Publish the recorded fault-injection recovery demonstration

**Date:** 2026-07-22
**Baseline commit:** `ab29a86947ee77de2e8d40275cc42143d898fffd` (`origin/main` == local `main`, clean worktree, no open PRs, M23 merged)

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` M21 (Section I) and §2 (mandatory reassessment
  gate paragraph).
- `DEVELOPMENT_PLAN.md` §8 (Decision Gates): G1's condition ("Stage 1
  gate passes and the documented pilot returns `go`") and its
  pass-action list ("Publish the recovery demonstration; start the
  `v0.2` train (M9-M12); open-source launch"). The `v0.2`/`v0.3`
  trains and open-source launch already happened; "publish the
  recovery demonstration" is the one G1 pass-action never separately
  completed.
- `FUTURE_ENHANCEMENTS.md` §8 ("Publish the recorded fault-injection
  demonstration — promoted to M21").
- `docs/pitch.md`: the "First public artifact" claim under "The short
  version" ("kill the coordinator mid-run, show reconciliation-driven
  recovery, show whether a duplicate effect was prevented ... publish
  the evidence bundle for independent inspection"); the "Observed
  interruption-and-recovery record" section (PR #86, 2026-07-19,
  against this repository, not the smoke fixture) and its named
  reusable fault-injection sequence; the "Pilot results (2026-07-20)"
  section's "Coordinator interruption" and "Ambiguous external
  -operation result" paragraphs (`round_to_nearest()` PR #39,
  `chunk_list()` PR #41, against `Mathews-Tom/enginery-provider-smoke`)
  and its `Result: go` line.
- `docs/operations.md` ("Running a Stage 1 workflow", "Composing a
  request", "Recovery semantics") and `docs/examples.md` (Example B) —
  the exact `stage1 build-request` / `stage1 start` / `stage1 watch
  --advance` / `stage1 review` / `stage1 evidence` command sequence
  this milestone must reuse verbatim, since PER-PR GATES forbids
  product-code changes.
- `tests/provider_smoke/test_github_omp.py` and
  `src/enginery/adapters/github.py`'s `GitHubAdapterConfig
  .require_smoke_repository` — confirmed the only allowlisted live
  -mutation target is `Mathews-Tom/enginery-provider-smoke`, and that
  repository is **private** (`gh repo view` confirms
  `"visibility":"PRIVATE"`).
- Live re-verification (2026-07-22, this session): `gh auth status`
  confirms an authenticated `Mathews-Tom` session with `repo`, `gist`,
  `workflow`, `read:org` scopes; `gh repo view
  Mathews-Tom/enginery-provider-smoke` confirms the repository exists,
  is reachable, and its default branch is `main`; `omp --version`
  reports `17.0.7` (installed, matching the harness this milestone's
  demo must reuse); `uv run enginery --version` reports `0.3.0`,
  matching `README.md`'s published status; `git status --short
  --branch` and `gh pr list --state open` confirm a clean `main` with
  no open PRs.
- One pre-existing gap, independent of this milestone's own scope,
  identical in shape to the one M20's and M23's own reassessment
  entries already recorded: `DEVELOPMENT_PLAN.md` M21 and
  `EXECUTION_PROMPTS.md`'s M21 `/goal` block both cite `.docs
  /strategy.md` §5 as the source of the "launch wedge artifact" framing
  and the "stated launch channels." A full `.docs/` directory listing
  confirms no `strategy.md` file exists today (only
  `BACKLOG_SEQUENCE.md`, `DEVELOPMENT_PLAN.md`,
  `FUTURE_ENHANCEMENTS.md`, `EXECUTION_PROMPTS.md`,
  `MILESTONE_REASSESSMENTS.md`, `handoff.md`), and a repository-wide
  search confirms `strategy.md` has never existed in this repository's
  git history at any commit. `docs/pitch.md`'s own already-published
  "First public artifact" paragraph (above) independently states the
  same "record and publish a fault-injection recovery demonstration"
  requirement `strategy.md` §5 would have supplied, so this milestone
  proceeds grounded in `docs/pitch.md` (a tracked, published document)
  instead of the missing `.docs/strategy.md` §5 for the artifact's
  *content* requirement. No tracked document names a specific external
  "launch channel," so the channel choice below is this reassessment's
  own decision, disclosed rather than invented from a vanished source.
  Not auto-corrected — noted for a human, matching M20's and M23's
  precedent of disclosure over silent workaround.

### Confirmed assumptions

- M21 depends only on M8, already externally merged; `RELEASE TARGET:
  none` and `RELEASE PREP: not-required` remain correct — this
  milestone changes no package version and runs no release script.
- Gate G1 already passed (`docs/pitch.md`'s `Result: go`,
  2026-07-20); this milestone does not re-run the pilot decision, it
  produces the one still-outstanding G1 pass-action.
- `Mathews-Tom/enginery-provider-smoke` remains the only repository
  this codebase's own discipline permits a live mutation against
  (`GitHubAdapterConfig.require_smoke_repository`); no new allowlist
  entry is added.
- No product code, port, adapter, or workflow changes are needed — the
  full Stage 1 command surface this demo reuses already shipped in
  `v0.1.0`-`v0.3.0`.

### Invalidated or refined assumption (the one that changed the design)

The plan's REQUIRED WORK line and SCOPE both describe publishing
"somewhere reachable outside this repository," and the objective
describes a "distinct published artifact separate from the pilot
record already embedded in `docs/pitch.md`." Because the only
allowlisted live-mutation repository is **private**, the real GitHub
issue/PR this demo creates there cannot itself be the published,
externally reachable artifact — a third party without access to that
private fixture repository could never open the link. This refines
"published artifact" into two distinct things instead of one: (a) the
real, private, retained GitHub issue/PR in
`Mathews-Tom/enginery-provider-smoke`, kept open and unmerged exactly
like every prior pilot run (`docs/pitch.md`'s own stop-before-merge
convention), serving as durable internal evidence an operator with
repository access could still audit; and (b) a self-contained, public
write-up — command transcript, timestamps, PIDs, evidence digest,
explicit statement of what was and was not observed — published to a
GitHub Gist under the same already-authenticated account (`gist`
scope already present in the live `gh auth status` check above), which
*is* reachable by a third party with no special repository access,
matching the milestone's own acceptance line ("reproducible by a third
party ... without special repository access beyond what `README.md`
already documents"). The Gist links the private PR's number for an
operator who does have access, but does not depend on that access to
be independently readable. This does not relax "not a scripted
narration of a past run" — the Gist is authored from a fresh live run
executed and recorded in this session (2026-07-22), reusing the exact
`stage1 build-request` / `stage1 start` / `stage1 watch --advance` /
`stage1 review` / `stage1 evidence` command sequence `docs/operations
.md` and `docs/examples.md` already document, not new tooling.

### Newly discovered risks

- A live run against `enginery-provider-smoke` dispatches a real OMP
  worker process that spends real LLM budget and wall-clock time
  (`docs/pitch.md`'s own pilot precedent: 159-191 seconds per Stage 1
  run). This is disclosed, not hidden, and matches the same class of
  spend `tests/provider_smoke` and the `v0.2`/`v0.3` pilots already
  incurred under the identical allowlist discipline.
- The published Gist must contain zero secret values. The credential
  -reference fields Stage 1 requests already use
  (`operator-gh-cli`, `operator-harness-session`) are opaque
  references by design (`docs/operations.md` "Configuration"), never
  literal tokens, so the command transcript itself is safe to publish
  verbatim; this reassessment still requires an explicit human check of
  the final transcript before publication, per the milestone's own
  HUMAN REVIEW GATE.
- Reusing `docs/pitch.md`'s exact reusable fault-injection sequence
  risks producing a write-up that reads as a restatement of already
  -published evidence rather than a fresh reproduction. The write-up
  must record this run's own distinct identifiers (issue number, PR
  number, branch suffix, worker PID, timestamps, evidence digest) so a
  reader can tell it is a new, independently reproducible execution,
  not a copy of the 2026-07-19/2026-07-20 pilot text.

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

- Substituting `docs/pitch.md`'s own already-published "First public
  artifact" paragraph for the missing `.docs/strategy.md` §5 as the
  content-requirement source, per the gap noted above.
- Splitting "the published artifact" into a private, retained GitHub
  issue/PR (internal durable evidence, unmerged, matching Stage 1's
  stop-before-merge contract) plus a public GitHub Gist (the externally
  reachable artifact `README.md`/`docs/pitch.md` link to), since the
  only allowlisted repository is private and cannot itself satisfy
  "reachable outside this repository."
- The "demo script or runbook" deliverable ships as a new tracked
  Markdown file under `docs/` (not a new Python script), composed
  entirely from the Stage 1 CLI commands `docs/operations.md` and
  `docs/examples.md` already document, consistent with PER-PR GATES'
  "No product code changes."
- Objective, 1-PR stack topology, target release (`none`), human
  review gate, and rollback (revert the doc-only PR; the Gist and the
  retained fixture-repo issue/PR are the only externally durable
  artifacts and are not deleted on rollback, matching how
  `docs/pitch.md`'s own prior pilot PRs were never retracted after
  publication) all remain exactly as written in `DEVELOPMENT_PLAN.md`
  M21 and `EXECUTION_PROMPTS.md`'s M21 `/goal` block.

### Result

**GO.**

### Human approval reference

Recorded via the `ask` tool in this session before any live-provider
action or code/doc changes, per `DEVELOPMENT_PLAN.md` §2: "every
assessment outcome, including GO, requires recorded explicit human
approval... before detailed design, branch creation, or code changes."

## M22 — Run and publish the Stage 2 + Stage 3 pilot comparison protocol

**Date:** 2026-07-22
**Baseline commit:** `510cc39a36967e621175954d09875cacf658b6c6` (`origin/main` == local `main`, clean worktree, no open PRs)

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` M22 (Section I) and §2 (mandatory reassessment
  gate paragraph, including the release-preparation grep requirement,
  which does not apply here since M22 is not a release-preparation
  milestone).
- `DEVELOPMENT_PLAN.md` §4 (Release Trains): M22 has `Target release:
  none`; §4's release rules confirm M12 already "publish[ed] a
  deliberately separate fixture distribution to prove the release
  provider. It must never consume the final product's name or
  version," and M13 "deploys only the controlled local service
  fixture" — the two disciplines this milestone must reuse exactly.
- `FUTURE_ENHANCEMENTS.md` §8 ("A second, independent pilot writeup
  post-`v0.3.0` — promoted to M22").
- `docs/pitch.md`'s "Comparison protocol and decision rule" and "A
  concrete pilot" sections (the exact go/no-go rule and evidence
  fields to reuse) and its "Pilot results (2026-07-20)" section (the
  level of write-up detail to match: per-item elapsed time, a named
  manual-baseline average vs. Enginery-path average, injected-fault
  paragraphs, evidence-bundle digests, an operator-burden paragraph,
  and an explicit `Result:` line applying the same decision rule).
- `docs/operations.md` — confirmed `stage2 status` remains the only
  Stage 2 CLI verb (read-only stack inspection); Stage 2's own
  merge/build/publish orchestration has no CLI surface. Stage 3 has no
  CLI surface at all (library-level only).
- `src/enginery/workflows/plan_to_release.py` (`Stage2ReleaseWorkflow`)
  and `src/enginery/engine/release_manifest.py`
  (`VersionChangelogBroker`, `ReleaseTarget`, `validate_release_target`,
  `_RESERVED_DISTRIBUTION_NAMES = frozenset({"enginery"})`) —
  confirmed the product-name collision guard and the
  already-recorded-version guard (`known_versions_from_changelog`)
  are still live, unmodified code that will hard-fail before any
  fixture publish that reused the product's name or a version already
  in the fixture's own `CHANGELOG.md`.
- `src/enginery/engine/fixture_build.py` (`FixtureBuilder`) and
  `src/enginery/adapters/pypi.py` / `src/enginery/adapters/github.py`
  (`PyPiAdapter`, `GitHubReleaseAdapter`) — confirmed the exact
  application-layer objects M12's live publish used (git log commit
  `b2efc9a`, PR #112 "m12/stage2-live-release-prep": "Version/changelog
  prepared by `VersionChangelogBroker.prepare()` ... using real GitHub
  API calls in this session"). No committed script performs this live
  publish end to end — `scripts/run_stage2_gate.py` only *verifies* an
  already-published release and refuses to run without
  `--fixture-distribution`. M12's original live publish was therefore
  driven by direct library calls in an interactive session, not a
  tracked script; M22 reuses the identical library objects the same
  way, since PER-PR GATES forbids new product code.
- `scripts/run_stage3_gate.py` — read in full. Its own docstring states
  it "touches only a local process and no external credential, so it
  is not opt-in gated: it runs as part of ordinary per-PR and full
  gates." Confirmed the full incident-to-hotfix-deploy-observe-rollback
  -restore narrative runs against a temp git repo and an ephemeral
  `127.0.0.1:<free-port>` instance of
  `fixtures/enginery-stage3-local-service/app.py`, and every resource
  (temp dir, spawned process, hotfix worktree) is torn down in a
  `finally` block. This is exactly the "Enginery path" for the Stage 3
  comparison and needs no new code — it already exists, live, and
  unmodified.
- Live re-verification (2026-07-22, this session): `git rev-parse
  HEAD` and `git status --short --branch` confirm a clean `main` at
  `510cc39`; `gh pr list --state open` returns none; `gh auth status`
  confirms an authenticated `Mathews-Tom` session (`gist`, `read:org`,
  `repo`, `workflow` scopes); `gh repo view Mathews-Tom/Enginery` shows
  the public product repository (`main` default branch); `gh repo view
  Mathews-Tom/enginery-provider-smoke` shows the private allowlisted
  fixture repository — confirmed still the only repository
  `GitHubAdapterConfig.require_smoke_repository()` permits a live
  mutation against, and not needed for this milestone since the Stage
  2 fixture release targets the public `Mathews-Tom/Enginery` repo
  exactly like M12's own `enginery-stage2-fixture-v0.1.0` release did
  (`gh release list` on `Mathews-Tom/Enginery` confirms that tag
  already exists, dated 2026-07-20, alongside the real `v0.1.0`
  -`v0.3.0` releases with a clearly distinct tag prefix). `curl
  https://pypi.org/pypi/enginery-stage2-fixture/json` returns `404`
  (the fixture has never touched real PyPI); `curl
  https://test.pypi.org/pypi/enginery-stage2-fixture/json` reports
  exactly one existing release, `0.1.0` — confirming M12's disposable
  -fixture-distribution discipline binds to **TestPyPI**, never
  `pypi.org`, and that `0.1.0` is already claimed and must not be
  reused. `UV_PUBLISH_TOKEN` is present in the environment (value not
  read or logged); `uv --version` reports `0.6.14`; `uvx twine
  --version` reports `6.2.0`, confirming both publish and
  destination-hygiene tooling this milestone needs are available.
  `fixtures/enginery-stage3-local-service/app.py` exists unmodified.

### Confirmed assumptions

- M12 and M13 are externally merged (confirmed by their PR merge
  commits already on `main`, `v0.2.0`/`v0.3.0` already published per
  `docs/pitch.md`'s own "Status update" paragraph). `RELEASE TARGET:
  none` and `RELEASE PREP: not-required` remain correct — this
  milestone changes no `enginery` package version and runs no product
  release script.
- The disposable-fixture-distribution discipline from M12 (never the
  product's name or version; TestPyPI only; a public GitHub Release on
  the product repository with a distinct `enginery-stage2-fixture-v*`
  tag prefix) is still current and still mechanically enforced by
  `validate_release_target`/`_RESERVED_DISTRIBUTION_NAMES` in code
  today, not just by convention.
- The controlled-local-service discipline from M13 (an ephemeral
  `127.0.0.1` process, never a real destination) is still current and
  is exactly what `scripts/run_stage3_gate.py` already exercises,
  unmodified, live, and outside opt-in gating by its own design.
- `docs/pitch.md`'s comparison protocol and go/no-go decision rule
  require no code change to reuse: "manual baseline: task input,
  operator actions, elapsed time, tests, review evidence, and any
  recovery step... Then run the same class through Enginery... Go:
  every injected stale-evidence case is rejected; no duplicate
  external effect occurs; the interrupted run resumes only after
  reconciliation; an independent reader can explain why the result is
  merge-ready or blocked from the evidence bundle; and the operator
  accepts the additional installation and maintenance burden."

### Invalidated or refined assumption (the one that changed the design)

The plan's SCOPE line describes "one real Stage 2 work item (a real,
disposable fixture release, mirroring M12's own disposable-fixture
discipline)." M12's own Stage 2 delivery bundled two separate concerns:
(a) a two-milestone plan merged root-to-leaf against the allowlisted
`Mathews-Tom/enginery-provider-smoke` repository through
`MergePolicyService`/`StackCoordinator`, and (b) the release
preparation-through-verification sequence
(`VersionChangelogBroker.prepare()` -> `FixtureBuilder.build()` /
`verify_clean_install()` -> `PyPiAdapter.publish()` /
`GitHubReleaseAdapter.publish()` -> destination `verify()`) this
milestone's own comparison question (manual release burden vs.
Enginery-orchestrated release burden) is actually about. Replaying (a)
for this milestone would mean opening two throwaway pull requests
against the smoke fixture purely to produce mergeable commits with no
content of their own — disproportionate live-provider cost for zero
comparison value, since the merge-scheduler mechanics are not what
differs between the manual and Enginery release paths.

This refines the Stage 2 "work item" to the release
preparation-through-verification segment alone. `Stack` merge
-readiness is supplied as a single already-`MERGED` synthetic slice
(position 1, bound to `fixtures/enginery-stage2-fixture`'s real current
commit as `head_revision`) rather than live PR merges, satisfying
`VersionChangelogBroker.prepare()`'s `constituent_work_merged` guard
honestly (every constituent slice really is merged — there is exactly
one, and it already reflects the real repository state) without
fabricating unrelated GitHub activity. Both the manual-baseline and
Enginery-path runs perform the identical deliverable: one new tagged
GitHub Release plus one new TestPyPI publish of
`enginery-stage2-fixture`, verified at both destinations — the same
class of task, run twice, exactly as `docs/pitch.md`'s own protocol
does for Stage 1's three function classes.

### Newly discovered risks

- PyPI/TestPyPI publication is irreversible once executed — a claimed
  version can be yanked but never reused. Mitigations already
  confirmed above: TestPyPI only (never `pypi.org`), the disposable
  `enginery-stage2-fixture` name (hard-blocked from ever equaling
  `enginery` by `_RESERVED_DISTRIBUTION_NAMES`), and two new version
  numbers (`0.2.0` for the manual-baseline run, `0.3.0` for the
  Enginery-path run) chosen specifically because they are unclaimed
  (`0.1.0` is the only existing release) and read unambiguously as
  fixture versions, not as a real `enginery` release a user could
  mistake for the product.
- The Stage 2 GitHub Release lands in the **public**
  `Mathews-Tom/Enginery` repository, visible next to the real
  `v0.1.0`-`v0.3.0` releases. This repeats M12's own already-published,
  already-reviewed pattern (`enginery-stage2-fixture-v0.1.0` already
  sits there with a clearly distinguishing tag prefix and a "Disposable
  ... fixture" description in its own `pyproject.toml`), not a new risk
  class — but it is the one destination requiring the milestone's own
  explicit HUMAN REVIEW GATE ("a human reviews the fixture-distribution
  name/version for collision with the real product") before either new
  tag is created, since collision-checking a name is comparatively easy
  to get wrong under time pressure.
- No committed script drives the live Stage 2 publish end to end (only
  `scripts/run_stage2_gate.py`, which verifies an already-published
  release). This milestone's "Enginery path" therefore runs as an
  uncommitted, throwaway driver script that imports and calls
  `Stage2ReleaseWorkflow`'s constituent objects directly in this
  session — never checked in, matching PER-PR GATES' "No product code
  changes" and mirroring exactly how M12's own live publish was
  originally performed (git log, above).
- `run_stage3_gate.py`'s narrative deliberately configures the hotfix's
  deployed build with `defect_mode="health_degraded"` to force the
  rollback branch — this is a fixed, reviewed, non-agent-authored
  narrative already shipped in `main`, reused unmodified; the Stage 3
  manual baseline must reproduce the identical forced-unhealthy
  narrative (not a different, easier one) so the two paths remain a
  comparable class of task per `docs/pitch.md`'s own protocol.
- Fixture package version/changelog edits
  (`fixtures/enginery-stage2-fixture/pyproject.toml`,
  `CHANGELOG.md`) are tracked, non-product files that must land via a
  real commit for the published release to point at a real,
  GitHub-reachable commit SHA (`GitHubReleaseAdapter.publish()` binds
  `target_commitish` to the exact pushed commit and rejects a mismatch).
  These land as commits on the single `m22/pilot-comparison-01` branch
  alongside the write-up, matching the milestone's own 1-PR stack
  depth rather than opening a separate PR as M12 did (PR #112) — a
  discrete, disclosed deviation from M12's own PR topology, not from
  its disposable-fixture-distribution discipline.

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

- SCOPE is refined as above: the Stage 2 "work item" is the release
  preparation-through-destination-verification segment of Stage 2, run
  twice (manual baseline, then Enginery-orchestrated), rather than a
  full two-PR plan-to-release cycle against the smoke fixture. This
  strengthens rather than weakens the comparison: it isolates exactly
  the mechanism this milestone's question is about.
- Both fixture version bumps (`0.2.0` manual, `0.3.0` Enginery-path)
  and the write-up land as commits on the single `m22/pilot-comparison
  -01` branch/PR, not a separate release-preparation PR, consistent
  with `DEVELOPMENT_PLAN.md` M22's own "Est. PRs | 1".
- Objective, target release (`none`), human review gate, merge
  discipline, and rollback (revert the single doc/fixture-chore PR;
  the two already-published TestPyPI versions and the GitHub Release
  are not retracted on rollback, matching how M12's own
  `enginery-stage2-fixture-v0.1.0` publication was never retracted and
  how M21's Gist/private-PR evidence was likewise left in place) all
  remain exactly as written in `DEVELOPMENT_PLAN.md` M22 and
  `EXECUTION_PROMPTS.md`'s M22 `/goal` block.

### Result

**GO**, conditioned on the milestone's own two explicit human gates
being separately satisfied before their respective live actions: (1)
this reassessment itself, before any live-provider or fixture-publish
action; (2) the milestone's named HUMAN REVIEW GATE confirming the
`0.2.0`/`0.3.0` fixture-distribution name/version show no collision
with the real product, immediately before the two new TestPyPI/GitHub
Release publishes execute.

### Human approval reference

Recorded via the `ask` tool in this session before any live-provider
or fixture-publish action, per `DEVELOPMENT_PLAN.md` §2 ("every
assessment outcome, including GO, requires recorded explicit human
approval... before detailed design, branch creation, or code changes")
and this milestone's own GLOBAL CONSTRAINTS ("Do not proceed even
after `GO` until a human approves the reassessment entry").

## M19b — Prepare, publish, and verify `v0.4.0`

**Date:** 2026-08-04
**Baseline commit:** `412b8f0d61d1ec4903373032edcfa6677cff6329` (`origin/main` == local `main`, clean worktree, no open PRs, no open issues)

### Documents and evidence inspected

- `DEVELOPMENT_PLAN.md` M19b (Section I), §2's 2026-08-04 release-train
  `DECISION`, §2's mandatory reassessment-gate paragraph including its
  release-preparation verification-tooling-completeness grep
  requirement (which does apply here — M19b is a release-preparation
  milestone), §4's new `v0.4.0` row and its release rules, §6's
  release-management bullets.
- `EXECUTION_PROMPTS.md` M19b `/goal` block.
- Live release state, verified rather than assumed: `pyproject.toml`
  `version = "0.3.0"`; `git tag -l` shows `v0.1.0`, `v0.2.0`, `v0.3.0`
  and the three `enginery-stage2-fixture-*` tags, and no `v0.4.0`;
  `gh release list` shows `v0.3.0` (2026-07-21) as the newest product
  release; the PyPI JSON API reports `0.3.0` latest with releases
  `['0.1.0', '0.2.0', '0.3.0']`. `v0.4.0` does not exist anywhere.
- Merge state: `gh pr list --state merged` confirms PRs #140–#146
  (M18 and M19) merged 2026-07-22, and #147–#150 (M20–M23) merged the
  same day. `gh pr list --state open` and `gh issue list --state open`
  are both empty.
- Release tooling, read and executed: `scripts/release_gate.py --help`,
  `scripts/full_system_gate.py --help`, `scripts/check_docs_currency.py`
  in full, and `scripts/release_gate.py`'s import and call sites for
  the docs-currency check (lines 29–30, 163–171, 188).
- `README.md` §Status and `docs/operations.md` version-declaring lines.

### Confirmed assumptions

- Both prerequisites hold. M18 and M19 are externally merged and
  `v0.3.0` is published to both destinations, so §4's preparation
  trigger for the `v0.4.0` train is satisfied.
- `scripts/release_gate.py` accepts `--version VERSION` for an
  arbitrary canonical version, plus `--skip-install-smoke` and
  `--evidence-out`. `--version 0.4.0` is a supported invocation. No
  gap.
- `scripts/full_system_gate.py --stages` accepts any combination of
  `'1'`, `'2'`, `'3'`, so `--stages 1,2,3` is supported unchanged.
  This is the first release-preparation milestone where this specific
  grep found no gap — M12b and M13b each had to add the flag first.
- `check_docs_currency.py` is genuinely wired into the release gate,
  not merely present: `release_gate.py` imports `run_check` and calls
  it as a gate step, and a failure fails the whole gate.
- `uv run python scripts/check_docs_currency.py` exits 0 today
  (`PASS docs-currency`) at canonical version `0.3.0`.
- M19b's stated 3-PR shape correctly anticipated that the doc sync
  must move inside the release stack rather than trail it as a
  follow-on, because the gate now fails on stale docs. That reasoning
  is sound; see the refinement below for why it is not yet sufficient.

### Invalidated or refined assumption (the one that changed the design)

M19b's plan text and `/goal` block both name one docs-currency risk:
that `check_docs_currency.py` might *false-positive* on `CHANGELOG.md`'s
and `docs/RELEASE_EVIDENCE.md`'s own historical `0.3.0` entries during a
`0.3.0` -> `0.4.0` transition. That risk does not exist. `EXCLUDED_DOCS`
(`check_docs_currency.py:54`) excludes both files wholesale, so a
historical entry cannot be examined at all.

The real defect is the opposite one: the check **false-negatives** on
the single most consumer-visible stale-version claim in the repository.

Falsified empirically rather than by reading, by calling
`_check_self_version_declarations(files, root, "0.4.0")` in-process
against the real 21-file tracked-markdown corpus at the baseline
commit, i.e. simulating the exact transition M19b will perform:

- At canonical `0.3.0`: 0 failures (matches today's passing run).
- At canonical `0.4.0`: **2 failures, both in `docs/operations.md`** —
  `:10` (`Enginery is \`v0.3.0\``) and `:57`
  (`package_metadata: enginery 0.3.0 installed`).
- **`README.md` produces 0 failures**, yet `README.md:13` reads
  "`v0.3.0`, published on PyPI and GitHub Releases (`v0.1.0`, `v0.2.0`,
  and `v0.3.0` are all live)". That is a current-version
  self-declaration in the repository's most-read file, and the check
  cannot see it.

Cause: the four `STALE_SELF_VERSION_PATTERNS`
(`check_docs_currency.py:61-66`) are literal sentence forms, not a
general self-declaration rule. README's phrasing is ", published on
PyPI"; the pattern requires " is published to PyPI".

A pattern-hit inventory over the whole corpus compounds this: two of
the four patterns match **nothing anywhere** —
`` `v(\d+\.\d+\.\d+)` is published to PyPI `` → 0 hits, and
`` `v(\d+\.\d+\.\d+)` \(Stage \d+ only\) `` → 0 hits. Both were written
against phrasings that the `v0.3.0` doc-sync correction had already
deleted, so they guard text that no longer exists. Effective coverage
of the check is two patterns over one file.

M20's own acceptance criteria are technically met and substantively
weak: "passes against the current, already-corrected doc set" is
trivially satisfiable by a check that matches nothing, and "fails
closed against a fixture doc" was proved only against synthetic
fixtures the check's own patterns were written to match. Neither
criterion exercises a real version transition against the real corpus.

### Newly discovered risks

- **Executing M19b as planned would publish `v0.4.0` with `README.md`
  still advertising `v0.3.0` as current**, and `release_gate.py` would
  report `docs_currency: passed` while doing so. That reopens exactly
  the documentation-staleness failure M20 was built to close, on the
  release most likely to be a new adopter's first contact — which is
  also the release whose entire purpose is to make the M18/M19 surface
  reachable by such an adopter.
- **Third recurrence of the release-tooling-completeness pattern**
  (M12b `--stages`, M13b `--stages`, now M19b docs-currency coverage).
  §2's grep requirement worked as designed and caught this before
  implementation. The unaddressed root cause is narrower and worth
  naming: a verification script whose acceptance was proved only
  against fixtures it was co-written with, never against the real
  artifact it will gate. Future checks of this class should carry an
  acceptance criterion phrased against the real corpus.
- The packaged surface merged 2026-07-22, thirteen days before this
  reassessment, so a stale local checkout could build a non-tip commit.
  Already named in M19b's Risks & rollback; unchanged and still live.
- No new unbounded external side effect. The irreversible actions
  remain exactly the `v0.4.0` tag, the PyPI publish, and the GitHub
  Release, all behind the milestone's existing human publication gate.

### Discrete changes to objective, scope, deliverables, acceptance, verification, stack, rollback

- **STACK DEPTH: 3 -> 4 PRs.** A separately-justified pre-release
  corrective PR is prepended, matching the remedy M19b's own text
  already authorizes for a tooling gap ("a separately justified
  pre-release corrective PR ahead of this stack").
- **PLANNED STACK becomes:**
  1. `fix(release): make the docs-currency check catch a real version transition`
  2. `build(release): prepare v0.4.0 version, changelog, and dependency manifest`
  3. `docs: sync README and operator docs to v0.4.0`
  4. `docs(release): finalize v0.4.0 compatibility statement and release notes`
- **SCOPE gains** (PR 1 only): generalize the self-version detection so
  a real `0.3.0` -> `0.4.0` transition fails closed on `README.md`;
  remove or replace the two patterns with zero corpus hits rather than
  leaving dead guards in place; add a regression test that asserts the
  detection against the **real tracked corpus** at a bumped canonical
  version, not against a synthetic fixture alone.
- **ACCEPTANCE strengthened, nothing weakened.** PR 1's regression test
  must be mutation-verified: it must fail against the pre-fix pattern
  set, not merely pass against the post-fix one. `README.md` must
  appear among the detected sites when canonical is `0.4.0` and docs
  are unsynced, and the check must report zero failures once PR 3's
  sync lands.
- **VERIFICATION gains** one step before PR 3 and one after: run the
  self-version check against the real tracked corpus at canonical
  `0.4.0` and require `README.md` among the failures beforehand, then
  require zero failures afterward.
- **Unchanged:** objective; target release `v0.4.0`; the consumer-side
  acceptance bar (a clean PyPI install of `0.4.0` must invoke all five
  M18/M19 commands from a scratch directory); `full_system_gate.py
  --stages 1,2,3`; the human publication gate; merge discipline and
  root-to-leaf topology; rollback (close the prep PRs before
  publication; a corrective version after).
- **Rollback for PR 1 specifically:** it touches only
  `scripts/check_docs_currency.py` and its test, neither of which is
  packaged into the wheel, so it can be reverted independently of the
  release without affecting published artifacts.

### Result

**GO with a mandatory scope amendment** — stack depth 3 -> 4, with the
docs-currency corrective PR first. The plan itself pre-authorized this
remedy for a tooling gap, so this is not `REPLAN REQUIRED`; but the
amendment must be recorded in `DEVELOPMENT_PLAN.md` M19b and the
`EXECUTION_PROMPTS.md` M19b block before any branch is created, so the
executed contract and the written contract agree.

Conditioned on two separate human gates: (1) approval of this entry,
before the plan amendment and before any branch or code change; (2) the
milestone's existing human publication gate, immediately before the
irreversible `v0.4.0` tag, PyPI publish, and GitHub Release.

### Human approval reference

**Granted 2026-08-04**, recorded via the `ask` tool in this session,
before the plan amendment and before any branch creation, per
`DEVELOPMENT_PLAN.md` §2 ("every assessment outcome, including `GO`,
requires recorded explicit human approval... before detailed design,
branch creation, or code changes") and this milestone's own GLOBAL
CONSTRAINTS ("Do not proceed even after `GO` until a human approves the
reassessment entry").

Approval text: *"Amend plan to 4 PRs. I'll start the M19b stack
development separately."* The scope amendment above is therefore
authorized and has been applied to `DEVELOPMENT_PLAN.md` M19b and the
`EXECUTION_PROMPTS.md` M19b block. Implementation of the four-PR stack
is explicitly reserved to a separate session; no branch was created in
this one.

The milestone's second gate — human publication approval immediately
before the irreversible `v0.4.0` tag, PyPI publish, and GitHub Release —
remains outstanding and is not covered by this approval.

## M24 — Make Gate G4 evidence measurable and fail closed

**Date:** 2026-08-06  
**Baseline commit:** `859de098299fd0a8d76ffc689dd72fa36fe3c07c` (`origin/main`, clean working tree before this planning amendment)  
**Decision:** `GO — PLAN REVISION: M24/M24b added`

### Evidence inspected

- Published `v0.4.0` release/tag and its GitHub Release record.
- The live installed-consumer Stage 1 pilot: one completed low-risk issue workflow in one repository, two pending outcome observations, zero recorded interventions, no registered human principals, and a G4 exit status of `3`.
- `src/enginery/adapters/github.py`: every GitHub issue is normalized as `WorkKind.ISSUE` and `RiskClass.LOW`.
- `src/enginery/workflows/issue_to_pr.py`: Stage 1 rejects every non-`ISSUE` work item.
- `src/enginery/evaluation/gate.py`: G4 requires two work kinds and two risk classes while its recurring-deficiency condition always reports `unmeasured`.
- `src/enginery/evaluation/gate_floor.py`: the existing roster is only a list of strings and does not bind a GitHub identity, role, authorization source, or an authenticated approval.
- `src/enginery/policy/approval.py`: the existing approval registry is in-memory and cannot be the durable dual-authority evidence required for a G4 finding.
- Current GitHub CLI documentation for `gh api` and pull-request review JSON.

### Invalidated assumptions

The prior plan treated G4's remaining blockers as exclusively organizational. That is false. More operations alone cannot produce a machine pass: the current GitHub Stage 1 route cannot yield a second work kind or risk class, and no possible evidence record can make the recurring-deficiency line pass.

### Approved scope and downstream impact

The owner selected:

1. Closed declared GitHub labels as the source of work-kind and risk-class evidence.
2. GitHub pull-request reviews as the authenticated dual-human record.
3. A bounded Stage 1 extension rather than a gate-only taxonomy.
4. A new `v0.5.0` train with M24 as implementation and M24b as release preparation.

M24 is limited to measurement integrity: source-bound labels, classified Stage 1 qualification, GitHub-mapped authority configuration, immutable deficiency evidence, GitHub evidence-PR verification, fail-closed gate reporting, migration, tests, and operator documentation. It cannot create a second repository, recruit a human, fabricate an intervention/outcome/deficiency, or start M14b/M15.

M24b owns the only `v0.5.0` version/changelog/tag/publication work. M14b and M15 remain blocked until M24 is externally merged and actual post-M24 evidence makes G4 pass.

### Human approval reference

**Granted 2026-08-06** through the interactive planning decisions in this session:

- “Approved. Proceed to scope a G4 remediation that makes dual-athority deficiency evidence and deversity criteria measurable.”
- Declared GitHub labels selected as the classification source.
- GitHub PR reviews selected as the authority-evidence source.
- Extending Stage 1 work kinds selected over a gate-only taxonomy.
- `v0.5.0` selected as the release train.
- “Approved. Proceed to begin implementation planning.”

This authorizes plan/prompt reconciliation only. M24 implementation still requires its own fresh pre-implementation design gate against the then-current default branch, followed by explicit human approval before branch creation or code changes.

## M24 — Make Gate G4 evidence measurable and fail closed

**Timestamp:** `2026-08-10T18:07:00Z`  
**Baseline commit:** `859de098299fd0a8d76ffc689dd72fa36fe3c07c` (`origin/main`, clean worktree)  
**Decision:** `GO — PLAN REVISION: none`  
**Trigger:** The owner authorized M24 execution after the 2026-08-06 remediation planning decision.

### Dependencies and released baseline verified

- `v0.4.0` exists as a local tag; its GitHub Release was published at `2026-08-03T22:23:08Z`, contains the wheel and sdist, and PyPI returns `200` for `enginery` version `0.4.0`.
- M18's merged gate implementation is present in PRs #140–#142. M8 Stage 1, M14a outcome capture, and the prior M4/M7 dependency surfaces are present on `origin/main`; the M24 baseline is the published `v0.4.0` release commit.
- The current gate command against `tests/fixtures/ledger.db` returns exit `3` and `overall: "fail"`: all registered floors are unset, there is no completed classified cohort, no interventions, no durable deficiency mechanism, no second repository, and no registered humans. It does not manufacture a pass.

### Documents, code, and provider evidence inspected

- The M24, M24b, M14b, and M15 entries and source-map rows; the current M24 and downstream execution prompts; and this reassessment ledger.
- `docs/design.md` domain, policy/authority, evidence, configuration, and staged-proof contracts; `docs/workflows.md` Stage 1 ownership and exact-revision workflow description.
- The current GitHub adapter, Stage 1 request/qualification/runtime paths, G4 CLI/evaluator/configuration, serialization, migrations, and their adapter/workflow/gate tests.
- GitHub's current REST documentation: issue labels are available through `GET /repos/{owner}/{repo}/issues/{issue_number}/labels`; pull-request reviews are chronological, paginated records exposing reviewer `user`, `state`, `commit_id`, and `submitted_at`.
- Live read-only API checks: `enginery-provider-smoke#7` returns an issue object with an empty label array, stable URL, and `updated_at`; merged Enginery PR #90 returns a closed/merged head and an empty review array. No fixture was mutated.

### Revalidated M24 contract

- Classification remains closed: exactly one case-exact `enginery/work-kind/issue` or `enginery/work-kind/plan` label and exactly one case-exact `enginery/risk/low` or `enginery/risk/medium` label. Unknown, duplicate, conflicting, missing, and case-variant labels in either declared namespace reject before mutation; non-classification labels remain outside this closed parser.
- The serialized source snapshot must retain canonical label strings, provider provenance, and a source revision/digest that changes when declared classification changes. The run's bound work-item digest and qualification source digest must include that data; legacy, unlabeled, or manually created requests cannot enter the G4 cohort.
- Stage 1 supports only labeled `issue` and `plan`; medium risk follows the existing human plan-approval route. `incident`, `milestone`, `factory_change`, high risk, new source providers, and Stage 4 remain excluded.
- The authority roster migrates to schema version 2 and binds each `AuthorityPrincipal` to an immutable GitHub numeric user ID, with the GitHub login retained only as diagnostic metadata. Schema version 1 must be rejected rather than interpreted as authority evidence.
- Evidence-review validation uses the merged PR's final head and the exact merged evidence-document digest. For each configured GitHub user ID, the chronologically latest review bound to that head must be `APPROVED`; two distinct approvers are required, and neither may equal the PR author or any cited-evidence producer. Missing or malformed review/PR payloads, pagination failure, stale head, dismissal/non-approval, or identity mismatch fail closed.
- A deficiency finding is a new immutable ledger aggregate. It must cite at least two distinct verified-complete classified runs and durable ledger references. All run volume, kind/risk breadth, intervention count, outcome completeness, and repository diversity derive from that same eligible cohort; no all-history counter can satisfy a G4 condition.

### Scope, release, and downstream impact

- Objective, scope, deliverables, acceptance, verification, five-PR stack, rollback, and `v0.5.0` release train are unchanged. No reconciliation prerequisite is required.
- M24b remains the only `v0.5.0` publication unit. It must prove the merged CLI/configuration surface from clean consumers without claiming G4 passed.
- M14b and M15 remain blocked until post-M24 operational evidence causes a persistent G4 report to pass. The gate language already requires the classified cohort, immutable dual-authority finding, two repositories, and GitHub-mapped principals.

### Implementation authorization

The current owner directive supplies explicit approval for this reassessment result and the unchanged five-PR M24 implementation stack. Product work may begin from `origin/main`; it must retain the fail-closed contract above and must not create or assert operational G4 evidence.

## M24 — Authority identity reconciliation

**Timestamp:** `2026-08-10T18:30:00Z`  
**Decision:** `REPLAN REQUIRED — PLAN REVISION: M24-I1`  
**Trigger:** The M24 implementation review found that the approved reassessment requires immutable GitHub numeric user IDs, while the authoritative M24/M24b/M14b/M15 text left the identity mapping ambiguous and one M24b consumer command name was stale.

### Evidence and revision

- GitHub review payloads expose a stable numeric `user.id` as well as a mutable login. Login-only authorization cannot establish the stable identity required by the M24 reassessment.
- The M24 plan now binds authority principals and reviews to numeric GitHub user IDs, retaining a login only as diagnostic data. Every cohort quantitative measure is explicitly constrained to the same verified classified cohort.
- M24b now names both shipped G4 recording commands. M14b and M15 now require the same numeric-identity evidence before their gate-deferred work can begin.

### Downstream impact and authorization

M24b, M14b, and M15 remain otherwise unchanged and blocked by their existing gates. This entry authorizes only the docs-only reconciliation prerequisite. The existing M24 implementation stack must not merge or receive further product-code work until that prerequisite is reviewed, green, externally merged, and a fresh M24 reassessment reports `DESIGN GO — PLAN REVISION: none`.

## M24 — Post-reconciliation implementation authorization

**Timestamp:** `2026-08-11T05:51:11Z`  
**Decision:** `DESIGN GO — PLAN REVISION: none`  
**Trigger:** PR #162 merged the M24-I1 authority identity reconciliation at `2240085dd5132775eaa5704a757a54a33cb8e2f7` after both platform gates passed.

### Evidence

- The published `v0.4.0` release remains a non-draft, non-prerelease GitHub release, published on `2026-08-03T22:23:08Z`.
- The merged authority contract requires immutable GitHub numeric user IDs for configured human principals and exact-head evidence-PR approvals. GitHub review payloads provide `user.id`; labels and reviews remain read through the documented GitHub issue-label and pull-review APIs.
- The reconciled M24, M24b, M14b, and M15 plan/prompt text consistently retains the closed source-label vocabulary, classified-cohort-only measurements, immutable deficiency finding, two-principal separation, Stage 4 deferral, and the `v0.5.0` release target.
- PR #162 contains no M24 product behavior or operational G4 evidence. The prior M24 branches require rebase onto this merged prerequisite before review or merge.

### Downstream impact and authorization

M24 product work may resume from `origin/main` under the reconciled five-PR stack. M24b remains the sole `v0.5.0` publication unit and M14b/M15 remain blocked until a persistent G4 report passes using genuine post-M24 operational evidence. This authorization does not assert G4 passage, create a human principal, or manufacture a classified cohort, intervention, outcome, or deficiency.

## M24b — Release-tool documentation-scope reconciliation

**Timestamp:** `2026-08-11T09:25:00Z`  
**Decision:** `REPLAN REQUIRED — PLAN REVISION: M24b-R1`  
**Trigger:** The M24b release-preparation gate inspected the merged release gate before version metadata changed and found an implementation-code path exception for planning artifacts.

### Evidence inspected

- `origin/main` is clean at `c866de7ecffc7dc029aed645501ceff24541d939`; M24 PRs #157, #166, #164, #165, and #161 plus procedure correction #167 are merged with successful macOS and Ubuntu CI.
- The published `v0.4.0` GitHub Release is non-draft and non-prerelease with matching wheel/sdist assets; PyPI serves `enginery==0.4.0` with the same hashes.
- The `0.5.0` release-gate invocation accepts the requested command and fails only because the canonical metadata remains `0.4.0`, as required before preparation.
- The currency gate introduced in reconciliation commit `2240085dd5132775eaa5704a757a54a33cb8e2f7` hard-codes planning-artifact paths in code and comments. That conflicts with the M24b prohibition on implementation-code, comment, docstring, and tracked-documentation references to those paths.
- The merged G4 CLI commands are `gate status --gate G4 --json`, `gate record-g4-deficiency`, and `gate record-g4-deficiency-evidence`; the schema-2 authority configuration binds principals to numeric GitHub user IDs. The operator procedure preserves the classified label vocabulary, medium-risk human approval, fail-closed G4 result, and M14b/M15 deferral.

### Plan and prompt change

- M24b-R1 adds the conditional docs-only reconciliation prerequisite and permits a narrowly scoped release-tool correction plus regression coverage in PR-1 before version metadata changes.
- M24b verification now requires public-documentation selection without planning-artifact path exceptions. The release target, M24-only train scope, command names, numeric identity migration, fixture requirement, consumer smoke, and M14b/M15 gate deferral are unchanged.

### Downstream impact and implementation authorization

M14b and M15 remain unchanged and blocked by a persistent G4 pass backed by genuine multi-repository, dual-human, intervention, outcome, and deficiency evidence. This entry authorizes only `docs(plan): reconcile M24b design`. No release-preparation code or release documentation may begin until that docs-only prerequisite is reviewed, green, and externally merged. After merge, repeat the M24b design gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

## M24b — Post-reconciliation release-preparation authorization

**Timestamp:** `2026-08-11T09:28:00Z`  
**Decision:** `DESIGN GO — PLAN REVISION: none`  
**Trigger:** PR #169 merged M24b-R1 at `230998fb4d448331cf64ca71a4e1f0443cd4f78c`; its fresh main CI passed on both macOS and Ubuntu.

### Evidence inspected

- Exact merged M24 surface: PRs #157, #166, #164, #165, and #161, plus procedure correction #167; their merged commits are ancestors of `origin/main`.
- Released baseline: `v0.4.0` is a non-draft, non-prerelease GitHub Release with wheel/sdist hashes matching the live PyPI `enginery==0.4.0` version.
- M24 command surface: `gate status --gate G4 --json`, `gate record-g4-deficiency`, and `gate record-g4-deficiency-evidence` are present; schema-2 authority configuration binds principal identities to unique numeric GitHub user IDs, preserving logins only as diagnostics.
- Release tooling: `release_gate.py --version 0.5.0` accepts the requested command and rejects the current checkout only for the expected canonical `0.4.0` metadata mismatch; the cumulative gate supports only shipped Stages 1–3, preserving Stage 4 deferral.
- Release scope: the changelog, release notes, README, operation procedure, G4 configuration, labeled fixture evidence, and published-consumer requirements remain coherent with M24-only `v0.5.0`; the G4 status remains fail-closed without genuine operational evidence.

### Plan and prompt sections changed

None. M24b-R1 already reconciled the sole material mismatch.

### Downstream impact and implementation authorization

Release-preparation implementation may begin from `230998fb4d448331cf64ca71a4e1f0443cd4f78c`. PR-1 is limited to the approved currency-selection correction, regression coverage, canonical metadata, changelog, dependency manifest, and artifact metadata. M14b and M15 remain blocked until genuine post-M24 multi-repository, dual-human numeric-identity, intervention, outcome, and deficiency evidence makes persistent G4 status pass. This decision does not create that evidence or authorize Stage 4 behavior.

## M24b — Release-gate execution-order reconciliation

**Timestamp:** `2026-08-11T09:36:00Z`  
**Decision:** `REPLAN REQUIRED — PLAN REVISION: M24b-R2`  
**Trigger:** The authorized PR-1 implementation traced the release gate's execution order after canonical-version preparation.

### Evidence inspected

- `release_gate.py` runs the public-documentation currency check before checking canonical metadata.
- The planned PR order updates canonical metadata in PR-1 and current public version declarations in PR-2. Therefore a post-PR-1 `release_gate.py --version 0.5.0` must stop at stale documentation; it cannot be a passing PR-1 verification.
- The same ordering correctly permits a pre-metadata probe to stop at the expected `0.4.0` metadata mismatch, and permits the complete release gate after PR-2's documentation sync.
- The R1 public-documentation selector correction, M24 command surface, numeric identity configuration, release target, labeled fixture smoke, published-consumer smoke, and M14b/M15 deferral remain unchanged.

### Plan and prompt change

- M24b-R2 assigns selection regression, lockfile, build, metadata, and hash evidence to PR-1.
- M24b-R2 assigns the passing docs-currency and full release-gate invocations to PR-2, after public version declarations synchronize.

### Downstream impact and implementation authorization

This is a material release-verification ordering correction. It authorizes only `docs(plan): reconcile M24b release-gate order`; no release-preparation code or release documentation may proceed until that docs-only prerequisite is reviewed, green, and externally merged. After merge, repeat the M24b design gate and require `DESIGN GO — PLAN REVISION: none`. M14b and M15 remain unchanged and blocked by a genuine persistent G4 pass.

## M24b — Post-R2 release-preparation authorization

**Timestamp:** `2026-08-11T09:39:00Z`  
**Decision:** `DESIGN GO — PLAN REVISION: none`  
**Trigger:** PR #171 merged M24b-R2 at `b426c0aec10b80bbb034d581b868de8454fc15c6`; fresh main CI passed on macOS and Ubuntu.

### Evidence inspected

- M24 PRs #157, #166, #164, #165, and #161 plus procedure correction #167 remain merged into `origin/main`; `v0.4.0` remains published at PyPI and in a non-draft, non-prerelease GitHub Release.
- The merged command surface remains `gate status --gate G4 --json`, `gate record-g4-deficiency`, and `gate record-g4-deficiency-evidence`; schema-2 configuration requires numeric GitHub identity bindings.
- The currency gate accepts the requested `0.5.0` invocation, evaluates current public documentation before metadata, and therefore requires PR-2's version sync before its passing release-gate run.
- The revised stack correctly sequences PR-1's selector regression, lockfile, metadata, build, and hashes before PR-2's docs-currency and full release gate. M24 scope, labeled medium-risk approval smoke, published-consumer surface, and M14b/M15 deferral are unchanged.

### Plan and prompt sections changed

None. M24b-R2 already reconciled the sole remaining material mismatch.

### Downstream impact and implementation authorization

Release-preparation implementation may begin from `b426c0aec10b80bbb034d581b868de8454fc15c6` under the revised four-PR stack. This decision authorizes no product behavior beyond the release-only work described in M24b, creates no operational evidence, and preserves G4's fail-closed state and M14b/M15 block.

## M24b — Independently-green PR-order reconciliation

**Timestamp:** `2026-08-11T09:44:00Z`  
**Decision:** `REPLAN REQUIRED — PLAN REVISION: M24b-R3`  
**Trigger:** The first authorized PR-1 dry run updated canonical metadata and demonstrated that the full quality suite correctly rejects stale current public version declarations.

### Evidence inspected

- After only `pyproject.toml` changed from `0.4.0` to `0.5.0`, the real current-documentation test failed at the README and two operator-documentation declarations. This is correct fail-closed behavior.
- Separating metadata from documentation therefore makes PR-1 impossible to green under the required full quality gate, even though deferring a passing release gate to PR-2 was already explicit.
- The safe topology keeps PR-1 as the release-tool correction only and moves canonical metadata, changelog, dependency manifest, and current public documentation into PR-2, where they become one coherent, independently green consumer contract.

### Plan and prompt change

- M24b-R3 replaces the incompatible metadata-first PR-1 with a correction-only PR-1.
- M24b-R3 makes PR-2 the canonical metadata and documentation synchronization PR, preserving four release-preparation PRs and all M24-only scope limits.

### Downstream impact and implementation authorization

This material correction authorizes only `docs(plan): reconcile M24b PR order`. No release-preparation code or release documentation may proceed until that docs-only prerequisite is reviewed, green, and externally merged. Repeat the design gate after merge and require `DESIGN GO — PLAN REVISION: none`. M14b and M15 remain blocked by genuine persistent G4 evidence.