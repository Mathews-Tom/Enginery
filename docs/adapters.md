# Adapter Authoring Guide

Enginery's core (`domain`, `application`, `engine`, `policy`, `evidence`,
`evaluation`) never imports a provider SDK and never sees a provider-native
object. Every external system — a work ledger, an agent harness, a
workspace backend, source control, validation, release/deployment, or a
capability registry — is reached through one of the typed `Protocol`
interfaces ("ports") in `src/enginery/application/`. This is the contract a
new adapter must satisfy, grounded directly in the real port definitions
and the two shipped harness adapters (`OmpHarness`, `ClaudeCodeHarness`) as
worked reference implementations.

## Package boundary

```text
domain/       Immutable domain types, state transitions, schemas, invariants
application/  Use cases and ports; coordinates domain behavior
engine/       Workflow scheduling, process managers, leases, recovery
ledger/       SQLite event store, projections, inbox, outbox, migrations
policy/       Action schemas, hard rules, approval digests, decisions
evidence/     Evidence contracts, verification, terminal claims
evaluation/   Outcomes, cohorts, replay, comparison, canaries
adapters/     Provider implementations and normalization boundaries
cli/          Commands, JSON output, JSONL streaming, exit codes
```

`domain` imports nothing else in this repository. `application` depends
inward on `domain` and outward only through declared ports. `engine`,
`policy`, `evidence`, and `evaluation` never import provider SDKs.
Provider-native objects enter and leave **only** through `adapters`. `cli`
owns presentation and invokes application services; it never implements a
domain transition directly. Verify a layer at any time with:

```bash
uv run python scripts/check_import_boundaries.py <layer>
```

## The port catalog

| Port (Protocol) | Module | First implementations | Required methods |
|---|---|---|---|
| `WorkLedgerPort` | `application/work_ports.py` | `LocalWorkLedger`, `GitHubWorkLedger` | `probe`, `fetch`, `publish_lifecycle`, `reconcile` |
| `HarnessPort` | `application/work_ports.py` | `OmpHarness`, `ClaudeCodeHarness`, `ScriptedHarness` | `probe`, `start`, `events`, `result`, `cancel`, `reconcile` |
| `WorkspacePort` | `application/work_ports.py` | `LocalWorkspace` (`local-git-worktree`) | `probe`, `create`, `retain`, `cleanup`, `reconcile` |
| `SourceControlPort` | `application/work_ports.py` | `LocalGit` (`local-git`) | resolve/change/branch/commit operations plus `probe`, `reconcile` |
| `PullRequestPort` | `application/work_ports.py` | `GitHubPullRequests` | `probe`, `create_or_update`, `get`, `evidence`, `merge`, `reconcile` |
| `ValidationPort` | `application/delivery_ports.py` | `LocalValidation` (`local-validation`) | `probe`, `validate`, `reconcile` |
| `ReleasePort` | `application/delivery_ports.py` | `LocalPublication`, `GitHubReleaseAdapter`, `PyPiAdapter` | `probe`, `publish`, `verify`, `reconcile` |
| `DeploymentPort` | `application/delivery_ports.py` | `LocalDeploymentFixture`, `LocalServiceDeploymentAdapter` | `probe`, `deploy`, `rollback`, `reconcile` |
| `CapabilitySourcePort` | `application/delivery_ports.py` | `LocalCapabilitySource`, `ArmoryCapabilitySource` | `probe`, `discover`, `resolve`, `fetch_closure` |

Every port shares the same identity/reconciliation shape, described next.

## Every adapter must

Per the system design's general adapter requirements:

1. **Expose identity, version, and capabilities.** `probe()` returns an
   `AdapterStatus` (`kind`, `availability`, `fingerprint`, `detail`). An
   `AVAILABLE` status *requires* a non-`None` `AdapterFingerprint`
   (`provider_id`, `provider_version`, `api_version` — currently
   `ADAPTER_API_VERSION = 1`, `capabilities`); an `UNAVAILABLE` or
   `MISCONFIGURED` status *requires* `fingerprint=None`. These are enforced
   invariants, not conventions — constructing an inconsistent `AdapterStatus`
   raises `InvalidInputError`.
2. **Validate configuration before use.** Adapter configuration dataclasses
   (`GitHubAdapterConfig`, `OmpAdapterConfig`, `ClaudeCodeAdapterConfig`,
   and so on) validate every field in `__post_init__` — a blank credential
   reference or executable name fails immediately, not on first use.
3. **Normalize provider data at the boundary.** Provider-native objects
   (a `gh` JSON payload, an OMP event line, a Claude Code `stream-json`
   record) are mapped into the port's own types
   (`WorkLedgerSnapshot`, `PullRequestSnapshot`, `NormalizedAdapterEvent`,
   and so on) inside the adapter module. Nothing provider-shaped crosses
   into `application`, `engine`, `policy`, `evidence`, or `evaluation`.
4. **Receive a persisted operation ID for each side effect** and implement
   the four-result reconciliation query: `reconcile(operation_id=...)`
   returns one of `ReconciliationResult.FOUND_MATCHING`, `NOT_FOUND`,
   `FOUND_CONFLICTING`, or `INDETERMINATE`. This is what lets the
   coordinator recover from an interruption without duplicating or losing
   a side effect (see [`docs/operations.md`](operations.md#recovery-semantics)).
5. **Classify failures** as permanent, transient, policy, authentication,
   rate-limit, conflict, or ambiguous (`enginery.domain.errors`'s
   `FailureClass` values) rather than letting a raw provider exception
   escape the adapter boundary unclassified.
6. **Emit redacted diagnostic events.** Harness output in particular is
   untrusted: `redact_credential_shaped_text` runs before any output is
   persisted, and a `HarnessOutput`/artifact carries a `RedactionClassification`.
7. **Provide focused contract tests** — see `tests/adapters/` for the
   pattern each shipped adapter follows, and the shared parametrized
   harness fixture described below for the two-harness case specifically.
8. **Never hide a fallback to another provider.** An unconfigured or
   unavailable adapter is a diagnostic failure the operator must resolve,
   never a silent substitution for a different one.

Do not add a general plugin/discovery framework before two real
implementations require the same extension point.

## Two independent harness adapters, side by side

`OmpHarness` and `ClaudeCodeHarness` (`src/enginery/adapters/omp.py`,
`src/enginery/adapters/claude_code.py`) are the reference pair for
"what a correctly shaped adapter pair looks like." Neither is mandatory or
a fallback for the other — a run's `execution_configuration` names exactly
one `harness_provider` (`"omp"` or `"claude-code"`); an unconfigured or
missing provider is a diagnostic failure. Both adapters:

- probe by running the CLI's own version command and report
  `AdapterAvailability.UNAVAILABLE` (CLI absent) or `MISCONFIGURED`
  (unexpected output) with `fingerprint=None`, never a fallback;
- normalize their own event schema into the same
  `NormalizedAdapterEvent` kinds (`STARTED`/`PROGRESS`/`DIAGNOSTIC`/`TERMINAL`),
  rejecting unknown or malformed lines as a classified failure rather than
  skipping them;
- redact output before artifact publication;
- have no CLI-level timeout flag — the coordinator's lease/heartbeat
  expiry calls the same cancellation entry point an operator would, so a
  timeout and an operator cancellation are the same code path and the same
  reported outcome;
- run through the same coordinator-supervised dispatch path;
- report harness/model metadata from observed output, never a hardcoded
  model ID.

A shared parametrized fixture (`tests/adapters/`) exercises
unavailable-harness diagnostics, malformed-output rejection, a clean run's
terminal status, and cancellation identically against both adapters, and
asserts that neither the shared task envelope (`HarnessTask`) nor the
shared running-worker identity type (`HarnessSession`) carries a
provider-named field. A third harness adapter should pass the same shared
fixture before being considered complete.

### Current support and what it does not claim

Supported harness providers are `omp` and `claude-code`, selected one per run. The shared adapter fixture and the real Stage 1/Stage 2 runs recorded in the release evidence are the extent of what is demonstrated; this guide makes no qualification, certification, or hosted-session guarantee beyond what those tests and recorded runs show. A harness, the model it reports, the session it opens, and the machine it runs on are four separate axes, and an adapter never infers one from another. Today both adapters record the model their own output reports (the Claude Code adapter from the assistant message's `model`; the OMP adapter from a completed assistant message's `provider` and `model`, joined as `provider/model`); neither reports the effective effort level, which is recorded as unknown; and worker-machine identity is not recorded. A harness is a worker, not a trust boundary: trusted-local operation is not containment. No upstream skill or source is vendored into the adapters, and reference projects are independently re-implemented patterns only.

### Trust modes and permission mapping

Each adapter launches its harness under one declared permission mapping, recorded in the retained runner receipt. In `trusted-local` mode the mapping is the explicit unconditional bypass the adapters have always used (Claude Code `--permission-mode bypassPermissions`; omp's headless default approval mode) and is labelled as such. In `isolated-oci` mode the harness runs inside the pinned worker image under a mapping that is never a bypass: Claude Code with `--restricted --permission-mode dontAsk --permission-prompts none` plus explicit allow and deny tool lists, and omp with `--approval-mode write` plus a read-only policy overlay in the image. The lists allow only local git commands and deny publishing and network commands; no test runner or interpreter is allowed, because those execute workspace code, and git hooks are disabled through command-scoped git configuration in the worker environment. These lists narrow the tool surface and are defence in depth, not the boundary: an allowed git command still honours repository-local configuration the worker can write. Containment is the boundary: the container holds no credentials, no remote and no route out. In the isolated mode the adapter hands the host-side launcher a launch spec instead of starting the harness itself, probes the harness version and protocol from the image rather than the host binary (a mismatch against the execution catalog holds the run exactly as host drift does), treats the worker's result file and receipt as untrusted input (bounded, redacted, validated), labels the model id the worker printed as worker-reported in the receipt, and refuses a launch whose permission mapping would be an unconditional bypass.

### Native model and effort controls

A task carries an execution selection. When it pins a model or an effort, each adapter submits the host's own flag (`--model` for both; `--thinking` for OMP, `--effort` for Claude Code) and rejects the task before launching any process if it cannot honor the control exactly. There is no fallback model, no substituted effort, and no cross-host equivalence between effort vocabularies.

- **Exact pins only.** An OMP model pin must be provider-qualified (`provider/id`); a Claude Code pin must be a full model name, not an alias such as `opus` or `sonnet`. An effort must be one of the host's own pinnable levels (OMP's `auto` delegates the choice to the host and cannot be pinned).
- **Accepted range, tested version.** Both hosts are in the `experimental` tier. For each host the catalog records two different things: the executable versions it accepts and the one version its flags were actually tested against. OMP is accepted at `>=18.10.3,<19.0.0` and was tested at 18.10.3 (`json-protocol=3`); Claude Code is accepted at `>=2.1.296,<3.0.0` and was tested at 2.1.296 (`stream-json-schema=1`). An installed version inside the range is accepted, and is reported as accepted and observed, never as tested. A range is exactly `>=LOWER,<UPPER` over plain numeric `MAJOR.MINOR.PATCH` versions; the catalog loader rejects any other operator, and a banner that is not plain numeric (a pre-release or build tag such as `18.11.0-beta.1`, or anything unparsable) is outside every range. Only the executable version is observed: the adapter runs the host's own version command and pairs the result with the protocol the adapter itself declares and parses, since the host does not report its stream protocol. The protocol must equal the catalog's. Before launching a task that pins a control, the adapter probes the installed host; a host outside the accepted range, with a different protocol, or that cannot be probed makes the control unsupported and the task is rejected. A task that pins nothing launches exactly as before and is never probed. Read-only diagnostics (`stage1 watch` without `--advance`, `stage1 evidence`) run the same version probe without creating any harness or artifact state. Catalog versions 1 and 2 are frozen history that name one exact version per host; a run bound to either keeps exact-version matching and holds on catalog drift. An isolated task is always probed inside the pinned image, pinned or not: the harness there must be exactly the catalog's tested version and protocol (the image recipe pins equal the catalog's tested versions, which a test checks; a version merely inside the accepted range does not qualify the image), a mismatch or an unprobeable image holds the run as `runner_fingerprint_drift` or `runner_unqualified`, and the verified version and protocol are recorded in the retained runner receipt, which collection refuses when they are missing or different.
- **Receipts.** Each execution records the control arguments actually submitted — extracted from the command that was launched, not from the selection — and the effective model the host reported (described by the catalog's evidence field), or an explicit unknown. It also records the host versions it answers, kept apart: the accepted range the run was bound to, the version the catalog tested, and the version observed when the dispatch command was built. That observation (recorded under the key `observed_executable_version_at_launch`) is the probe that proved the host acceptable while the command was built; for queued work it is therefore the version seen when the work was queued, not when its process later started. It is kept in the persisted worker command, so a later collection reads it back and never probes a host that may have been upgraded since. It is none when no such observation was recorded. The observed version is never reported as tested, and a change between the version observed at bind time and the one observed when the command was built that stays inside the accepted range is recorded, not held. The diagnostics add a third, separate observation: the version the binding check saw when the report was produced, which can differ from the command-build observation if the host was upgraded in between. The requested value is never copied into the effective one. The launched controls must equal the selected ones exactly: a missing pin or an extra model-steering flag such as `--fallback-model` holds the run. A selection declared strict-effective is held, not passed, when the effective model is unknown or differs, including when the host reports more than one model. A strict requirement on an effort pin is rejected before launch, since neither host reports the effective effort.
- **Isolated requests never probe the host binary.** `stage1 build-request --runner isolated-oci` does not run the operator's own `omp` or `claude` when it binds a pinned model or effort: the pinned image's harness is what runs, and it is probed from the image when the run is launched. A pinned control is therefore not refused because the host binary is outside the accepted range, and no host version is recorded as observed at bind time. A trusted-local request keeps the host probe described above.
- **Fingerprints are not qualification.** A capture of the launch arguments, or a matching fingerprint, never raises a host above `experimental`.

## Worked example: writing a new `WorkLedgerPort` adapter

Sketch (eliding validation and error handling already covered above; see
`src/enginery/adapters/github.py`'s `GitHubWorkLedger` for the complete,
real version):

```python
from enginery.application.adapter_types import (
    AdapterAvailability, AdapterFingerprint, AdapterStatus, ProviderKind,
)
from enginery.application.work_ports import WorkLedgerPort, WorkLedgerSnapshot, LifecycleProjection
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

class MyTrackerWorkLedger:
    """A WorkLedgerPort implementation for a new issue tracker."""

    def probe(self) -> AdapterStatus:
        # Run the tracker CLI/SDK's own version check; never assume availability.
        ...
        return AdapterStatus(
            kind=ProviderKind.WORK_LEDGER,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="my-tracker", provider_version="1.4.0", api_version=1,
            ),
            detail="my-tracker is available",
        )

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        # Fetch, then map provider-native fields into WorkItem/WorkLedgerSnapshot.
        # Provider-specific labels/states/users are mapped here, not exposed upward.
        ...

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        # Publish idempotently under operation_id; return FOUND_MATCHING on success.
        ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        # Query deterministic provider markers for the given operation_id.
        ...
```

This satisfies `WorkLedgerPort` structurally (Python `Protocol` types are
duck-typed — no base-class inheritance is required) and mirrors the shape
every other port implementation follows.

## The Armory capability-registry relationship

Armory (`Mathews-Tom/armory`) is a catalog and distribution surface for
skills, agents, hooks, rules, commands, utilities, and presets — it equips
agents; Enginery directs engineering. Enginery never depends on Armory for
its domain model or execution kernel:

```text
Armory equips agents.
Enginery directs engineering.
Agent harnesses perform assigned work.
Repositories and delivery systems receive verified outcomes.
```

`ArmoryCapabilitySource` (`src/enginery/adapters/armory.py`) implements
`CapabilitySourcePort` against Armory's real, current interface: a
`manifest.yaml` catalog of package directories on GitHub, with no MCP
server, no per-package signature, and no checksum, license, or dependency
field of its own. Four facts about this adapter matter operationally:

- **It is not a runtime dependency.** Importing the module needs only the
  standard library; every network-reaching method requires the optional
  `armory` extra (`uv sync --all-extras` installs it). `probe()` reports
  `AdapterAvailability.UNAVAILABLE` when the extra is absent — the engine
  keeps working with Armory disabled, exactly like an absent OMP or Claude
  Code CLI. **No shipped CLI command constructs or calls this adapter in
  this release** — using it today means writing Python against
  `CapabilitySourcePort` directly, the same way `tests/adapters/armory`
  exercises it.
- **Every read is bound to one immutable commit.** The adapter resolves its
  configured ref (default `main`) to a commit sha once, or takes an explicit
  40-character `revision`, and reads the catalog, the recursive file
  listing, every package file, and the repository `LICENSE` at that sha
  through the injected fetcher. The descriptor's `revision` is that sha. It
  never executes or imports fetched content.
- **A package is its complete closure.** The closure is every file under the
  catalog `path`. A symlink, submodule, or other non-regular tree entry
  rejects the package; the closure builder additionally rejects unsafe
  paths and unreviewed asset types (see "Package closures and the lock"
  below), so a package that ships a `.pyc`, database, or image is
  unlockable rather than silently hashed. Armory packages carry no
  Enginery manifest, so the adapter synthesizes a minimal one (identity
  from the catalog row, kind `instruction`, no permissions, no
  dependencies) and includes it in the closure; it grants no authority and
  declares no command, so an Armory package is installed as inert files and
  is never run by Enginery without approval. A package's license is decided
  by the license-like files it ships at its root when it has any, else by
  the repository `LICENSE` at the pinned revision — declared metadata only.
  After NFKC normalization and case-folding, a root file is license-like
  when its name contains `licen`, `copying`, `notice`, `unlicense`,
  `copyright`, `eula`, `legal`, `terms` or `patent`, contains `gpl`, `lgpl`,
  `agpl`, `mpl` as a whole word (optionally with a version) or `copyleft`,
  or is not ASCII; each
  one is a metadata asset of the closure, never an instruction. A
  license-like name is accepted only as one ASCII-letter word, or that word
  followed by one or two words from a closed list (`mit`, `apache`,
  `apache2`, `bsd`, `isc`, `lesser`, `gpl`, `lgpl`, `agpl`, `mpl`, `third`,
  `party`, `notices`, `thirdparty`) joined by `.`, `-` or `_`, optionally
  followed by `.md`, `.txt` or `.rst` (`LICENSE`, `LICENSE.MIT`,
  `LICENSE-APACHE2`, `COPYING.LESSER`, `THIRD_PARTY_NOTICES.md`), and its
  content must decode as UTF-8; anything else (`notice.so`,
  `license-helper.sh`, `licensed.pth`, `license.lua`, `license.html`, a
  name that is still non-ASCII after normalization) is an unreviewed asset
  type and fails the closure. A full-width name such as `ＬＩＣＥＮＳＥ.md`
  becomes ASCII under NFKC and is accepted. Any path containing a Unicode
  format, line-separator or paragraph-separator character is rejected. A
  license file is classified only by exact match with a canonical MIT,
  Apache-2.0 (with or without its appendix), BSD-2-Clause, BSD-3-Clause,
  ISC, 0BSD, Zlib, Unlicense or CC0-1.0 text after case-folding,
  heading-marker removal, removal of strictly shaped copyright lines in the
  leading block (and the Apache appendix's
  `Copyright [yyyy] [name of copyright owner]` line) and whitespace
  collapsing. A package that declares any other license id and ships its
  license text is held as `unknown`. A removable copyright line is at most
  80 characters: `copyright`, `(c)` or `©`, a year, year range or list, or
  `[yyyy]`, and an optional holder of one to five tokens. A token is
  letters and digits with at most one internal `.`, `'` or `-` and an
  optional trailing `.` (an initial or abbreviation), or `&`; `and` is an
  ordinary token. The holder may be followed by `, ` and exactly one closed
  suffix (`Inc`, `Ltd`, `LLC`, `GmbH`, `Corp`, `Co.`, `PBC`,
  `and contributors`, `et al.`) and a final `.`. A full stop may only end
  the line or follow a trailing initial or corporate suffix; a stop
  followed by whitespace and more text makes the line non-removable, as
  do `!`, `?`, other
  commas, dashes between words, colons, semicolons, a terms word,
  `All rights reserved`, or an other-license marker. The leading block ends
  at the first non-blank line that is neither the license title nor such a
  copyright line, and at most three copyright lines in it are removed (a
  fourth makes the text `unknown`); copyright lines anywhere else stay and
  break the match. A text
  with a Unicode format character, a line or paragraph separator, a control
  character, an HTML comment or any non-ASCII character other than `©` is
  `unknown`, as is any other text (inserted or appended terms, carve-outs,
  dual licensing, other licenses), including every license-like file whose
  name is not canonical (`license`, `licence`, `copying` or `notice` with no
  extension or `.md`, `.txt` or `.rst`) and whose text is not a template.
  **Residual risk:** classification is a fail-closed screen, not
  authentication. A short name-shaped phrase of at most five tokens on a
  copyright line (for example `Copyright 2024 Foo resale needs consent`,
  at most three such lines) cannot be told apart from a holder name, so it
  may be removed and the text still classify as a template; every
  unauthenticated package therefore still
  needs exact-digest human approval, where the approver reviews its license
  files. The same declared-license check runs for every source in the
  resolver: when a closure ships root license-like files, the declared
  (manifest) license is kept only if every file classifies to that same
  SPDX id and no `NOTICE` conflicts; otherwise the license is `unknown`.
  Every deciding file must classify to one SPDX id equal to the repository
  license, else the license is `unknown`, as is an unreachable repository
  license, and `unknown` holds redistribution. A canonical `NOTICE` file
  never decides the license, but one that names another license family or
  marker (GPL, LGPL, AGPL, Affero, general public license, MPL, SSPL,
  Creative Commons, `CC-BY`, share-alike, non-commercial, Commons Clause,
  EUPL, CDDL, Eclipse Public License, BUSL, Business Source License,
  Elastic License, EPL, PolyForm, OSL, copyleft, proprietary, dual
  licensing, "at your option", "choose either") or reserves
  all rights without a permissive grant, or one with a hidden or non-ASCII
  character, makes it `unknown`. Every
  fetched file, including the catalog `manifest.yaml` and the repository
  `LICENSE`, is verified against its git blob sha in the pinned tree: a
  mismatched or absent `LICENSE` is `unknown`, and a mismatched
  `manifest.yaml` or package file is an integrity failure.
- **Every capability it returns is `provenance="armory"`, which
  `CapabilityResolver` classifies as `unauthenticated`.** Content
  addressing (Enginery's own fetch-and-hash step) is not the same as
  trust: Armory supplies no cryptographic signature today, so every
  Armory-sourced capability a run introduces requires interactive
  exact-digest human approval (the `capability.materialize` policy action)
  before that run can execute it — the same hard rule that governs a
  repository-local capability added or changed by the active run. There is
  no "authenticated-by-TLS" fast path; TLS or transport identity alone is
  explicitly insufficient.

## Package closures and the lock

A capability is a package directory, not a single file. Every
`CapabilitySourcePort` returns the package's complete file list
(`fetch_closure`) and a `CapabilityDescriptor` whose `digest` is the
source's claim of the package's canonical **closure digest**. The resolver
rebuilds the closure from the fetched files and rejects a source whose
files do not hash to the claimed digest.

- **Manifest.** Each package ships `capability.json` at its root: a strict,
  versioned JSON document declaring `name`, `version`, `kind`
  (`instruction`, `command`, `hook`, or `adapter`), an optional SPDX
  `license`, transitive `dependencies` pinned to `name`, `version`, and
  `closure_digest`, `permissions` from a closed vocabulary, and
  `compatibility` (adapter API range and platforms). Unknown and duplicate
  keys are rejected. The manifest is itself an asset of the closure, so
  every field it declares is covered by the digest.
- **Closure.** Every file is recorded as a normalized relative POSIX path,
  an asset kind (`instruction`, `reference`, `script`, `data`, `metadata`),
  a SHA-256, a size, and an executable bit. The closure digest covers all
  of those and every dependency pin in a fixed order, so a changed sibling
  script, reference, data file, executable bit, or dependency pin changes
  the digest.
- **Rejected content.** Symlinks, hardlinked or non-regular files,
  absolute, `..`, backslash, and non-NFC paths, duplicate or case-colliding
  paths, executable bits on non-scripts, and any file outside the reviewed
  layout (root `*.md`, `instructions/*.md`, `references/` docs,
  `scripts/` `.py` `.sh` `.js` `.mjs` `.ts`, and `data/`, `schemas/`,
  `evals/` data files) fail with an explicit `CapabilityError` subclass,
  as do packages beyond 512 files, 2 MiB per file, or 16 MiB in total.
- **Lock.** A lock entry binds the closure digest to the origin (source
  label and immutable revision), license, kind, permissions, compatibility,
  dependency pins, and the trust classification. All of these are bound
  into `CapabilityLock.digest()`, so changing any of them supersedes an
  approval recorded against the old digest. A digest never proves
  publisher authenticity: only a signature that verifies against a pinned
  publisher key yields `authenticated`, and even then a run-introduced
  capability still needs exact-digest human approval. Each transitive
  dependency is a lock entry of its own, checked against its pin and
  approved and license-checked package by package.
- **License.** Only SPDX identifiers on the reviewed permissive list
  (`0BSD`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `CC0-1.0`, `ISC`,
  `MIT`, `Unlicense`, `Zlib`) are redistributable. An absent, `unknown`, or
  unreviewed license holds redistribution for every package the run
  introduces, and materialization refuses it even with an approval.
- **Materialization.** A verified closure is written once, as a whole
  directory, under its closure-digest path and never overwritten; every
  later use re-verifies the directory (no missing, changed, or extra file,
  no symlink) against the lock entry.
- **Lock schema.** The lock file declares `schema_version: 2`. A lock
  without a `schema_version` is the previous single-blob schema: it decodes
  read-only as legacy entries that carry no closure, cannot be
  materialized or approved, and must be re-resolved —
  `enginery capability lock --check` reports each such entry as needing
  re-resolution and consumers of the lock refuse it.

Repository-local capabilities (`LocalCapabilitySource`) remain valid
without any registry — "the engine works with Armory disabled" is an
enforced acceptance property, not an aspiration.

## The incident-to-hotfix-and-rollback surface

Stage 3 (production incident to verified hotfix and rollback) is a
shipped, adversarially-tested surface with no CLI command of its own:
using it today means writing Python against `IncidentService` directly,
the same way `scripts/run_stage3_gate.py` and the Stage 3 leg of
`scripts/full_system_gate.py` exercise it.

`IncidentService` (`src/enginery/incidents/service.py`) is a frozen
dataclass over one `LedgerService`, an optional `DeploymentPort`, and an
optional `PolicyEvaluator` — reconstructing it from a durable ledger
handle after a coordinator restart is the same pattern
`Stage1RunService`/`Stage2ReleaseWorkflow` already use. It drives one
closed-lifecycle `Incident` aggregate through `ingest` (idempotent —
re-ingesting the same `(source_provider, external_reference)` pair
returns the already-recorded incident rather than duplicating it),
`classify`, `bind_release_lineage`, `begin_reproduction` /
`attempt_reproduction` (only records `reproducing` from a real,
caller-supplied, executed check — never a hand-typed claim), the fixed
hotfix workflow, `execute_deployment` / `execute_rollback` (each its own
hard-required-human `PolicyAction` — `deployment.execute` and
`deployment.rollback` — so a deployment approval never satisfies the
rollback requirement), `resolve_observation`, and `record_follow_up`
(separate `WorkItem`, never merged into the emergency PR's own scope).

`enginery.incidents.hotfix` provides the fixed-broker repair path:
`create_hotfix_worktree` (a real git worktree at the affected release
revision), `apply_repair` (writes and commits a caller-authored
`HotfixRepair`), `prove_non_vacuous_regression` (runs the same real
check against the unfixed and repaired revisions — a repair is only
accepted if it fails on the former and passes on the latter),
`emergency_pull_request_request`, and `remove_hotfix_worktree`.

`LocalServiceDeploymentAdapter` (`src/enginery/adapters/local_service.py`)
is the real `DeploymentPort` implementation Stage 3 deploys through: it
starts, stops, and version-swaps a genuine subprocess running a
stdlib-only local HTTP service fixture
(`fixtures/enginery-stage3-local-service/app.py`) and polls its real
`/health` and `/version` endpoints — never a canned or simulated
receipt. This is distinct from `LocalDeploymentFixture`
(`src/enginery/adapters/local.py`), an in-memory fixture used only by
local dev/test composition, not by the real Stage 3 workflow.

## Example workflows

See [`docs/examples.md`](examples.md) for two complete, runnable worked
examples: a fully local, no-live-provider Stage 1 request (the same
composition `scripts/full_system_gate.py` uses for the cumulative
restart/replay gate), and a real GitHub/OMP-backed request (the same shape
[`docs/migration-sage-dev.md`](migration-sage-dev.md) uses to bring a
historical ticket into Enginery).
