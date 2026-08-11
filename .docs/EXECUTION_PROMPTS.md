# Execution Prompts — Enginery

**Regenerated:** 2026-07-19, from the approved [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md); amended 2026-08-06 after the owner-approved M24 measurable-G4-evidence remediation and its `v0.5.0` train. This revision retains the human-approved mandatory reassessment gate and records M8's coordinator-owned progression, retained-workspace, and fenced terminal-cleanup corrective contract.

Run one `/goal` block in a fresh session. Each block assumes its listed dependencies are externally merged to the default branch. Before any unstarted milestone from M6 onward begins, complete its mandatory reassessment gate, update the plan and these regenerated prompts after human approval, then execute the approved contract. Milestone stacks are independent across milestones and dependency-ordered within a milestone. This file is a derived artifact: regenerate it whenever `DEVELOPMENT_PLAN.md` changes rather than hand-patching individual blocks.

The architecture is harness-agnostic. OMP and Claude Code are reference adapters used to falsify provider-specific assumptions; neither is part of the domain model.

Source documents referenced below live in this repository:

- Planning documents (this directory, `.docs/`): `DEVELOPMENT_PLAN.md`, `MILESTONE_REASSESSMENTS.md`, `03_SYSTEM_DESIGN.md`, `02_PRODUCT_DIRECTION.md`, `04_SPECIFICATION_REVIEW.md`, `analysis.md`, `strategy.md`
- Finalized product documents (published, `docs/`): `docs/design.md`, `docs/overview.md`, `docs/pitch.md`, `docs/workflows.md`

These are the planning repository's own paths; a `/goal` block executes against the separate greenfield implementation repository recorded by the M1 identity gate, so each block below carries the planning-repository path explicitly.

`.docs/` is globally gitignored planning material and must never be committed to the implementation repository, referenced by filename, or cited by section number (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. Every milestone block's GLOBAL CONSTRAINTS section repeats this rule directly so it survives being pasted into a fresh session on its own.

Release trains, in execution order:

| Train | Milestones | Starts when |
|---|---|---|
| `v0.1.0` | M1–M8, M14a, M16, M17 | Immediately (M1 has no dependency) |
| `v0.2.0` | M9, M10, M11, M12, M12b | Gate G1 passes — Stage 1 gate complete and the documented pilot returns `go` |
| `v0.3.0` | M13, M13b | `v0.2.0` train's Stage 2 gate passes; M13b additionally waits for `v0.2.0`'s publication (M12b) |
| `v0.4.0` | M18, M19, M19b | Published 2026-08-03; the released consumer surface is the baseline for M24 |
| `v0.5.0` | M24, M24b | M24 depends on the published `v0.4.0` baseline; M24b prepares and publishes only the merged M24 surface |
| Gate-deferred | M14b, M15 | M24 externally merged and measurable Gate G4 passes — never by elapsed time |
| No train | M20, M21, M22, M23 | Repository tooling and published evidence, never packaged into the wheel; all four already merged — see `DEVELOPMENT_PLAN.md` §4 and Section I |

> **Gate-deferred milestones (M14b, M15):** the prompts below exist so the stack is ready to run only after M24 has made the gate measurable and recorded evidence shows G4 passed. Do not execute them before then. Each carries a gate-verification step as its first required action; an executing agent that cannot produce recorded evidence that G4 has passed must stop and report `NO-GO — GATE G4 NOT PASSED` without proceeding to implementation.

> **Published/backlog milestones:** M18/M19 are published in `v0.4.0`; M20–M23 are on no train and must never update the public version or `CHANGELOG.md`. M24 is a new `v0.5.0` correction that makes G4 measurable but does not satisfy its operational conditions. M24b is the only `v0.5.0` release-preparation unit.

## M1 — Confirm project identity and establish the repository scaffold

```text
/goal
You are executing M1 of the Enginery development plan: confirm project identity and establish the repository scaffold.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md, M1 and §§1–4, 6
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§1–4, 16–18
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/overview.md §§2, 6–8

TARGET
Create the new greenfield Apache-2.0 Python 3.12 repository and its verified package/CLI skeleton. Do not implement workflow behavior.

RELEASE TARGET
- Target release: v0.1.0.
- This milestone uses the canonical pre-release version 0.0.0.dev0.
- It must not create CHANGELOG.md entries for v0.1.0, tag, publish, or prepare a release.

LOCAL CONTEXT PATHS
- Approved product documents: /Users/druk/WorkSpace/AetherForge/Enginery (`.docs/` for planning documents, `docs/` for finalized product documents)
- Current folder is not automatically the final repository. Use the human-approved repository path and remote recorded by the identity gate.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Read the applicable skills before acting. The product name and package/CLI spelling are approved; do not reopen naming unless an availability conflict forces a new human decision.
- Before creating a remote repository or package code, obtain an interactive human decision for the GitHub owner and repository URL. Confirm that the approved Python distribution, import package, and executable name `enginery` remain available.
- Record the approved product name `Enginery`, the `enginery` package/CLI identities, repository values, and version once in a canonical project-identity source. Generate and verify dependent metadata; do not duplicate mutable identity or version values.
- Use uv only. Use built-in Python generics, from __future__ import annotations, and mypy --strict.
- Modular monolith boundaries: domain imports nothing outward; application may import domain; infrastructure/adapters/CLI depend inward.
- No framework, plugin system, database, agent adapter, mock product behavior, placeholder implementation, or hosted UI.
- Use Conventional Commits unless the new repository establishes a stricter convention.

SCOPE
- Human repository-ownership gate and immutable identity decision record.
- Apache-2.0 license and repository metadata.
- pyproject.toml, uv.lock, src layout, test layout, package boundaries, central error taxonomy, deterministic clock/ID test helpers.
- CLI entry point with --version and doctor skeleton that reports only implemented local prerequisites.
- Ruff, format check, mypy strict, pytest, macOS and Ubuntu CI.
- Security and contribution files needed to accept an open repository safely.

STACK DEPTH
4 PRs, dependent root to leaf. Keep each PR independently green.

PLANNED STACK
1. chore(project): record identity and initialize licensed uv package
2. feat(core): establish package boundaries and typed primitives
3. feat(cli): add version and diagnostic command surface
4. ci: enforce lint type and test gates on macOS and Ubuntu

REQUIRED WORK
- Resolve the repository owner and URL GAP before PR 1. Use the approved `Enginery` product name and `enginery` distribution, import, and executable identities. If an identity is unavailable or the owner is undecided, return NO-GO; do not invent another brand.
- Write one canonical identity/version source. Add a verification script that detects drift across product metadata, Python distribution, import package, CLI entry point, and repository URL.
- Add import-boundary tests and the smallest real CLI smoke tests.
- Confirm every configured command runs from a clean uv environment.
- Search for a repository PR template after remote creation; use it for every PR.

PER-PR GATES
- Run the tests relevant to that PR.
- Run uv run ruff check ., uv run ruff format --check ., uv run mypy --strict src, and uv run pytest -q before publishing each PR.
- Verify the branch contains only its intended concern and its parent is the previous stack branch.

REVIEW LOOP
- Review each PR locally for correctness, package-boundary leakage, unsafe defaults, and documentation drift.
- Address configured automated reviewer feedback that appears on its own. Never solicit or @-mention an external reviewer.
- Re-run the changed PR's focused tests and the full local gate after every amendment.

MERGE DISCIPLINE
- Read the stacked-prs skill before touching git topology.
- After the repository-ownership gate, initialize the repository on the human-approved default branch; use origin/main unless the remote declares another default.
- Exact topology: default branch -> m01/scaffold-01 -> m01/scaffold-02 -> m01/scaffold-03 -> m01/scaffold-04.
- Validate the first parent against the actual remote default and validate the current source branch before creating PRs.
- Merge root to leaf only with fresh green CI on the current head. Retarget each child to the default branch, force fresh CI when retargeting does not trigger it, then merge.
- Delete merged stack branches locally/remotely and verify a clean default branch.

RELEASE PREP
- RELEASE PREP: not-required.
- Do not create a v0.1.0 changelog, tag, GitHub Release, or PyPI artifact.

FINAL VERDICT
- GO only if the repository owner and identity record are approved, all four PRs are externally merged, the clean default branch passes the full local gate on macOS and Ubuntu CI, project-identity verification passes, and no downstream architecture was implemented prematurely.
- NO-GO for unresolved repository ownership, unavailable or drifting identity, unmerged work, stale/failing CI, duplicated metadata, import-boundary leakage, or release preparation.

RETURN
Return the final identity values, repository URL/path, PR and merge links, exact verification commands/results, release target v0.1.0, RELEASE PREP state not-required, and GO or NO-GO with blockers.
```

## M2 — Implement domain types, state machines, and workflow manifests

```text
/goal
You are executing M2: implement provider-neutral domain types, complete state machines, and validated workflow manifests.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M2
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§4–5, 7, 12, 15, 17
The target implementation repository is the identity recorded by M1.

TARGET
Encode the inner domain contract without persistence, scheduling, provider SDKs, or workflow execution.

RELEASE TARGET
v0.1.0. Keep 0.0.0.dev0. No changelog, tag, build publication, or release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- M1 must be externally merged and origin/main clean/green.
- Use uv, Python 3.12 typing, immutable value objects, fail-fast validation, and existing package boundaries.
- No adapter or provider types in domain models. No abstract interface without a current consumer in this milestone.
- Operation IDs exclude attempt number and are stable across retries.
- Preserve every transition and terminal semantic from design §10; do not simplify the state machines.

SCOPE
All domain IDs, digests, aggregates, state enums, transitions, failure classes, immutable workflow manifests, typed node declarations, input/output schemas, retry/budget declarations, evidence requirements, side-effect/idempotency metadata, parallel groups, child workflow declarations, and schema versioning.

STACK DEPTH
5 PRs.

PLANNED STACK
1. feat(domain): add identifiers values and work items
2. feat(domain): add runs attempts artifacts decisions and outcomes
3. feat(domain): enforce work run attempt and factory-change transitions
4. feat(workflow): validate manifests nodes and operation identities
5. test(domain): add invariant serialization and compatibility fixtures

REQUIRED WORK
- Implement every valid and invalid edge as data or guarded behavior, not prose.
- Reject unknown states, cycles, missing node schemas, unreachable terminal claims, invalid retry/budget values, undeclared side effects, and embedded general-purpose programs.
- Version serialized domain and manifest schemas; create golden compatibility fixtures.
- Derive operation identity from run, node, side-effect kind, target scope, and ordinal only.
- Add import-boundary checks proving domain independence.

PER-PR GATES
Run focused tests plus uv run ruff check ., uv run ruff format --check ., uv run mypy --strict src, and uv run pytest -q. Each fixture must assert behavior, not source text.

REVIEW LOOP
Review each PR for provider leakage, invalid-state reachability, serialization ambiguity, accidental mutability, and allocation-heavy copies. Address only organic reviewer feedback; never trigger bots. Re-run gates after amendments.

MERGE DISCIPLINE
Read stacked-prs first. Base is origin/main containing merged M1. Exact topology: origin/main -> m02/domain-01 -> m02/domain-02 -> m02/domain-03 -> m02/domain-04 -> m02/domain-05. Verify current source and first parent before mutation. Publish and merge root-to-leaf with fresh CI after each retarget; clean branches afterward.

RELEASE PREP
RELEASE PREP: not-required. No v0.1.0 metadata changes.

FINAL VERDICT
GO only if all five PRs are externally merged, every state-machine and manifest invariant has a behavioral test, schema fixtures are stable, domain has zero outward imports, and the full gate passes. Otherwise NO-GO.

RETURN
Return merged PRs, schema versions, invariant coverage summary, commands/results, release target, RELEASE PREP state, and GO/NO-GO.
```

## M3 — Build the SQLite event ledger and artifact store

```text
/goal
You are executing M3: build crash-safe SQLite workflow persistence and a content-addressed artifact store.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M3
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§3–4, 6–7, 9, 11, 14, 17
Use the M1 repository identity for the implementation target.

TARGET
Make domain state durable, transactionally consistent, replayable, inspectable, migratable, and restorable. Do not execute workflows.

RELEASE TARGET
v0.1.0; retain 0.0.0.dev0. No release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- M2 is externally merged.
- SQLite is authoritative for runtime state. Filesystem storage holds content-addressed artifact bytes; neither conversations nor repository files are runtime state.
- One transaction must cover expected aggregate versions, events, artifact metadata references, lease/scheduling updates, process-manager updates, projections, inbox acknowledgement, and outbox rows for one command.
- Never persist raw credentials. No catch-and-continue migration behavior.

SCOPE
Schema/migrations; append API; aggregate versions; local commit sequence; correlation/causation IDs; command inbox; transactional outbox; projections; process-manager state; artifact metadata and bytes; sensitivity classes; backup/restore; projection rebuild; consistency/doctor commands.

STACK DEPTH
5 PRs.

PLANNED STACK
1. feat(ledger): add schema migrations and atomic event append
2. feat(ledger): add command inbox outbox and process-manager state
3. feat(ledger): add projections replay and commit cursors
4. feat(artifacts): add content-addressed storage and redaction boundary
5. feat(storage): add verification backup restore and fault tests

REQUIRED WORK
- Define forward migrations and failure behavior before writing application repositories.
- Test expected-version conflict, commit rollback, interrupted write, corrupted event, missing artifact, digest mismatch, failed migration, backup during idle state, restore, and projection rebuild.
- Ensure raw harness/provider payloads cannot enter the ledger before adapter-side normalization/redaction.
- Add a fault-injection executable that proves atomicity and replay determinism. Build this as the shared fault-injection framework foundation (`DEVELOPMENT_PLAN.md` cross-cutting §"Verification economies") — later milestones parameterize it rather than writing independent harnesses.

PER-PR GATES
Run focused storage tests and the full uv/Ruff/mypy/pytest gate. Use temporary databases; tests must be deterministic and parallel-suite safe.

REVIEW LOOP
Inspect transaction boundaries, fsync/rename assumptions, migration irreversibility, secret persistence, N+1 projection queries, and silent recovery. Use the silent-failure review discipline for every exception path. Never trigger external reviewers.

MERGE DISCIPLINE
Read stacked-prs first. Base origin/main with M2 merged. Topology: origin/main -> m03/ledger-01 -> m03/ledger-02 -> m03/ledger-03 -> m03/ledger-04 -> m03/ledger-05. Validate first parent/source, publish, verify fresh CI, merge root-to-leaf, retarget safely, then clean branches.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if all five PRs are externally merged; atomic command, replay, projection, migration, artifact, backup, and restore fault tests pass; credential-shaped fixtures never persist; and the clean default branch passes all gates. Otherwise NO-GO.

RETURN
Return schema version, merged PRs, fault scenarios, backup/restore evidence, commands/results, release target, RELEASE PREP state, and verdict.
```

## M4 — Implement policy, approval, evidence, and terminal contracts

```text
/goal
You are executing M4: implement the closed policy model, human approvals, evidence verification, and terminal-state contracts.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M4
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§4–5, 8–9, 11–13, 17
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §8 (Single-operator authority model) — implement the producer-separation vs. dual-human-separation distinction exactly as stated; do not implement a single undifferentiated "distinct approver" rule.

TARGET
Make autonomy explicit, default-deny, independently auditable, and incapable of passing terminal claims with missing, stale, all-non-applicable, self-approved, or validation-weakening evidence.

RELEASE TARGET
v0.1.0; no release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- M3 externally merged.
- Hard rules are closed, non-overridable, and enforced below workflow code.
- policy.override, evidence.non_applicability.accept, review_finding.waive, factory_change.canary, and factory_change.promote always require an interactive human distinct from the requesting run/producer.
- factory_change.canary and factory_change.promote additionally require two distinct human principals (dual-human separation, design.md §6) — a single-operator deployment can implement the schema and the check but cannot itself complete this path; the check must fail closed when only one human principal is registered, not silently pass.
- Approval digests bind every policy-relevant input with explicit nulls; changes supersede prior decisions.

SCOPE
Action schemas; default-deny evaluator; policy results; hard-rule implementation; override handling; approval channel; canonical digest; evidence contracts/results; waiver and non-applicability decisions; merge-ready and released contracts; positive implementation evidence; risk classes and explanations; the producer-separation and dual-human-separation principal check.

STACK DEPTH
5 PRs.

PLANNED STACK
1. feat(policy): add action schemas and default-deny evaluation
2. feat(policy): enforce hard rules overrides and approval digests
3. feat(evidence): evaluate evidence contracts and positive implementation proof
4. feat(evidence): enforce merge-ready and released terminal contracts
5. test(governance): add adversarial authority and evidence suites

REQUIRED WORK
- Implement all design §15 hard rules exactly, including candidate isolation, held-out secrecy, publication/migration canary limits, and run-introduced capability approval.
- Distinguish pass, fail, and indeterminate. Indeterminate required evidence blocks.
- Reject empty-diff/all-non-applicable merge-ready claims and stale subject evidence.
- Implement the two-class principal-separation check from design.md §6: producer-separation (single distinct principal, satisfiable by one human operator approving run/agent-produced output) and dual-human-separation (two distinct registered human principals, required only for factory-change canary/promotion). A deployment with fewer than two registered human principals must be able to execute every producer-separation action and must be blocked, by design, from dual-human-separation actions.
- Add generated and randomized adversarial fixtures so the gate cannot pass by matching canned strings.

PER-PR GATES
Run policy/evidence tests, adversarial scripts, and full uv gates. Every hard rule needs at least one allow-boundary test and one blocked bypass test.

REVIEW LOOP
Use independent safety, silent-failure, and type-design review lenses. Check actor identity, approval supersession, action namespace closure, digest completeness, waiver authority, principal-separation class correctness, and terminal falsifiability. Do not trigger bots.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M3. Topology: origin/main -> m04/policy-01 -> m04/policy-02 -> m04/policy-03 -> m04/policy-04 -> m04/policy-05. Validate source/parent. Merge root-to-leaf only after fresh CI, then clean.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if all five PRs merge and every adversarial policy/evidence bypass is rejected by behavior, not fixture text, including a fixture proving a single-principal deployment cannot execute a dual-human-separation action. Any default-allow, self-approval, stale evidence, hard-rule override, or empty implementation pass is NO-GO.

RETURN
Return merged PRs, hard-rule matrix, adversarial results, commands, release state, and GO/NO-GO.
```

## M5 — Implement coordinator, scheduler, supervision, and workspace isolation

```text
/goal
You are executing M5: implement coordinator epochs, readiness scheduling, worker supervision, leases, cancellation, and git-worktree isolation.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M5
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§4–5, 7, 14–15, 17

TARGET
Execute deterministic fixture nodes safely across coordinator and worker crashes without duplicate workers, stale writes, workspace collisions, or blind retries.

RELEASE TARGET
v0.1.0; no release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- M4 externally merged.
- One coordinator epoch is the sole aggregate writer. Workers never write the ledger.
- Run workspace reservation and node execution lease are separate.
- Human waits hold no live node lease or child process.
- The worktree backend is not hostile-code containment. Support current POSIX macOS/Linux semantics only.

SCOPE
Coordinator epoch/heartbeat; typed command inbox consumption; readiness scheduler; resource/concurrency limits; fencing tokens; worker supervisor; PID/process-start/process-group identity; worktree reservations; create/retain/cleanup; cancellation propagation; orphan quiescence; resume/reconcile; deterministic node fixture; process-fault support for the shared fault-injection framework established in M3.

STACK DEPTH
6 PRs because lease, supervisor, workspace, cancellation, and recovery risks require isolated review.

PLANNED STACK
1. feat(engine): add coordinator epochs and command processing
2. feat(scheduler): add readiness dependency and resource scheduling
3. feat(engine): add fenced node leases and worker result ingestion
4. feat(workspace): add run reservations and git-worktree backend
5. feat(supervisor): add process groups heartbeat and cancellation
6. test(recovery): add orphan resume and concurrency fault matrix

REQUIRED WORK
- Persist coordinator and node lease ownership transactionally.
- Prove stale epoch and stale lease tokens cannot commit.
- Monitor coordinator heartbeat from a supervisor independent of the worker.
- Before re-lease, prove prior process group absent and workspace quiescent; otherwise block for human reconciliation.
- Test crash at every state boundary, PID reuse, heartbeat expiry, concurrent cancellation, cleanup failure, restart during human wait, and two runs targeting one repository.

PER-PR GATES
Run focused engine/workspace tests, platform-specific fault scripts, scheduler stress, and full uv gates. Do not weaken timing assertions to suppress races; use controllable clocks and synchronization points.

REVIEW LOOP
Use systematic debugging for every flaky/race failure. Review process identity, lock ownership, lease fencing, cancellation ordering, workspace retention, and cleanup evidence. Do not trigger external reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M4. Topology: origin/main -> m05/runtime-01 -> m05/runtime-02 -> m05/runtime-03 -> m05/runtime-04 -> m05/runtime-05 -> m05/runtime-06. Validate topology. Merge root-to-leaf with fresh CI per retarget and clean branches/worktrees.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if six PRs merge, stress/fault tests show zero duplicate leases/workspace collisions/stale writes, human waits have no live process, ambiguous quiescence blocks, and CI is green on macOS/Ubuntu. Any nondeterministic recovery test is NO-GO.

RETURN
Return merged PRs, supported platforms, fault matrix, stress counts, commands/results, release state, and verdict.
```

## M6 — Define adapter contracts and deterministic local providers

```text
/goal
You are executing M6: define provider-neutral adapter contracts and implement deterministic local providers.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M6
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§2–4, 10–11, 14–15

TARGET
Create enforceable application ports and local implementations before introducing external SDKs. Do not add speculative plugin infrastructure.

RELEASE TARGET
v0.1.0; no release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M5 externally merged.
- Provider-native types stop at adapter boundaries.
- Every side effect uses stable operation identity and native idempotency or deterministic reconciliation returning found_matching, not_found, found_conflicting, or indeterminate.
- Normalize, redact, digest, and sensitivity-classify before ledger persistence.
- Missing providers fail loudly; never auto-fallback.

SCOPE
Typed ports for work ledger, harness, workspace, source control/PR, validation/CI, release/deployment, and capabilities; provider capability/version fingerprint bound durably to the run with a fail-closed resume guard; normalized errors/events; shared contract suites; local ledger, scripted harness, git, validation, artifact publication, deployment fixture stub, and local capability source. Correct the root README's implementation-status statement in the final slice using only claims established by merged behavior.

STACK DEPTH
5 PRs.

PLANNED STACK
1. feat(adapters): define versioned capabilities errors and event envelopes
2. feat(adapters): define work harness workspace and SCM contracts
3. feat(adapters): define validation release deployment and capability contracts
4. feat(adapters): implement deterministic local providers
5. test(adapters): enforce reconciliation redaction and compatibility contracts

REQUIRED WORK
- Let concrete local implementations shape each port; delete any abstraction with no second consumer or clear shared contract use.
- Add contract fixtures for availability, capability negotiation, fingerprint migration and drift before provider calls, cancellation, malformed output, reconciliation outcomes, redaction before ledger persistence, and version incompatibility.
- Correct the root README in PR 5; it must make no implementation-status claim contradicted by M2–M6.
- Keep plugin entry points deferred until two real implementations require runtime discovery. The second harness (M9) and Armory (M10) are gated on Stage-1 usage friction (gate G1); do not pre-build their abstractions here beyond what these local fixtures require.

PER-PR GATES
Focused adapter contract/local tests plus full uv gates and import-boundary checks.

REVIEW LOOP
Review protocol usefulness, optional capability handling, provider leakage, secret handling, silent fallback, and reconciliation completeness. Use type-design and silent-failure analysis. Never solicit bot review.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M5. Topology: origin/main -> m06/adapters-01 -> m06/adapters-02 -> m06/adapters-03 -> m06/adapters-04 -> m06/adapters-05. Validate, publish, fresh-CI merge root-to-leaf, clean.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if all PRs merge, every local provider passes shared contracts, no provider object leaks inward, reconciliation has four explicit results, redaction precedes persistence, and no speculative plugin system exists. Otherwise NO-GO.

RETURN
Return adapter API version, provider matrix, merged PRs, commands/results, release state, and verdict.
```

## M7 — Integrate GitHub and OMP

```text
/goal
You are executing M7: integrate GitHub issues/PR/checks and OMP worker execution as the first real providers.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M7
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§2, 10–12, 14
- Current OMP headless/structured-output documentation and current GitHub API documentation; verify them before coding.

TARGET
Make real external state available through M6 contracts while preserving source revision, side-effect identity, normalized evidence, and credential boundaries.

RELEASE TARGET
v0.1.0; no release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M6 externally merged.
- Use a dedicated GitHub test repository named by configuration and accepted only when it exactly matches a version-controlled static allowlist. Missing repository or credentials must produce an explicit `not-run` smoke result with no provider mutation.
- Never expose token values to the ledger, logs, prompts, workspaces, command arguments, or configuration serialization. Store and use credential references only.
- External ambiguity reconciles before retry. CI evidence binds exact head SHA.
- No merge or release behavior.

SCOPE
GitHub issue snapshots and lifecycle projection; PR create/update/query; head/base/review/check metadata; deterministic PR markers; OMP probe/start/structured and raw event normalization/cancel/final status; opaque credential references; static smoke-repository allowlist validation; provider diagnostics; opt-in real smoke tests.

STACK DEPTH
5 PRs.

PLANNED STACK
1. feat(github): ingest revisioned issues through the work-ledger contract
2. feat(github): manage and reconcile pull requests and head metadata
3. feat(github): normalize checks reviews and mergeability evidence
4. feat(omp): implement harness execution events cancellation and results
5. test(providers): add contract and allowlisted real smoke gates

REQUIRED WORK
- Inspect current APIs before selecting dependencies.
- Recheck issue bound fields, base SHA, PR head, and CI conclusion.
- Simulate timeout after external success and prove adoption rather than duplication.
- Store raw output only as redacted/sensitivity-classified artifacts.
- Make live smoke tests explicit, skippable without being reported as passed, and self-cleaning or diagnostically retained.
- Pin discovered GitHub API and OMP JSON protocol capabilities in diagnostics; reject malformed or unsupported provider records rather than inferring missing state.
- Run the inherited M6 adapter baseline from its merged test locations, not a nonexistent contract-test path.

PER-PR GATES
Focused unit/contract tests and full uv gates. Run live smoke only with explicit credentials, a configured repository that exactly matches the static allowlist, and a cleanup/retention receipt. A skipped smoke is `not-run`, not passed.

REVIEW LOOP
Review auth precedence, pagination, rate limits, webhook/poll ordering, duplicate PR avoidance, stale checks, OMP malformed events, cancellation, and secret redaction. Do not trigger reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M6. Topology: origin/main -> m07/providers-01 -> m07/providers-02 -> m07/providers-03 -> m07/providers-04 -> m07/providers-05. Validate and merge root-to-leaf with fresh CI; reconcile any retained live fixtures before cleanup.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if five PRs merge, contract suites pass, the opt-in GitHub/OMP smoke succeeds against an allowlisted test repository, ambiguous success creates no duplicate PR, exact-head evidence is retained, and no secret persists. Missing live credentials produce NO-GO for the milestone gate, not a fake pass.

RETURN
Return provider versions/capabilities, smoke repository, created/cleaned external artifacts, merged PRs, test results, release state, and verdict.
```

## M8 — Deliver issue to merge-ready pull request

```text
/goal
You are executing M8: deliver the complete Stage 1 issue-to-merge-ready-PR workflow. This is the actual v0.1.0 product claim — everything after M8 in this train (M14a, M16, M17) packages and ships what M8 proves.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M8
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§9, 12–15, 17 (Stage 1 row)
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §9 (merge-ready contract, including the documented residual TOCTOU window between the final double-read and the terminal commit)

TARGET
Take one real allowlisted GitHub issue through a coordinator-owned progression loop: qualification, isolated OMP work, validation, review, bounded repair, PR/CI, fenced terminal cleanup, and a falsifiable merge-ready evidence bundle. Stop before merge.

RELEASE TARGET
v0.1.0; no release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M7 externally merged.
- Before activating Stage 1, run the approved live-provider preflight against the static allowlist and retain evidence that the allowlisted repository has a currently observable exact-head `CI` result, opaque credential references, OMP capability, and cleanup/reconciliation work. The disposable preflight fixture must not be used as the Stage 1 gate PR.
- Medium/high work requires human final review.
- At least one acceptance criterion requires positive implementation evidence tied to non-empty diff unless the work ends no_change_required.
- Re-read source and repository state before the first side effect and before terminal evidence. Where the provider supports a conditional/precondition request (for example an ETag or If-Match), bind the terminal claim to the observed subject version rather than relying on the double-read alone.
- Never merge the PR in this workflow.

SCOPE
Versioned workflow manifest; qualification; escalation; risk routing; plan approval when policy requires; CI-capable live-provider preflight; retained worktree; OMP node; focused tests; independent review; repair routing; PR update; exact-head CI wait; source divergence; cancellation/resume; fenced terminal cleanup; CLI/event/evidence presentation; Stage 1 live gate.

STACK DEPTH
3 corrective PRs.

1. feat(workspace): retain implementation workspaces through terminal Stage 1 ownership
2. feat(stage1): progress one ledger-derived Stage 1 action per `watch` invocation
3. test(stage1): prove recovery, terminal cleanup, and the retained real issue gate

- Build one coordinator-owned Stage 1 progression service from existing engine, policy, and adapter services. Expose only its lifecycle through `enginery stage1 start`, `watch`, `approve`, `reject`, `cancel`, `resume`, and `evidence`. Persist a run projection, every manifest-node transition, every external-operation intent, and every workspace transition before its side effect; do not introduce a second scheduler, an in-memory workflow coordinator, or a live script that hand-assembles helpers.
- Treat `start` as a durable intent, `watch` as reconciliation plus exactly one safe progression action, and every non-start command as a fenced transition against an existing run. A replacement process derives the action only from the ledger, current provider observations, existing runtime leases, and the workspace reservation. It reuses the recorded operation ID or fails closed; it never dispatches a duplicate operation.
- Retain the implementation workspace after result collection until the run is terminal. Validation, review, repair, PR/CI, and verification use that reservation. A terminal, cancelled, or failed run releases it only through one fenced cleanup transition; cleanup failure records a retained reconciliation state rather than masking the failure.
- Route no-op/all-non-applicable work to `no_change_required` or human rejection. Treat helper-only contracts and passive read-only lifecycle commands as insufficient.
- Represent repair as a fresh fenced attempt that re-enters validation; represent cancellation and human wait/resume from every relevant node through declared terminal transitions.
- Bind every required GitHub review and CI observation to the exact current PR head. Fail closed on stale reviews, duplicate contradictory checks, source/base/head divergence, ambiguous PR creation, and unavailable provider evidence. Preserve the documented residual double-read-to-terminal-transition race as an explicit evidence limitation; never call it a merge authorization.
- Before any Stage 1 run, execute the existing allowlisted GitHub/OMP preflight fixture with opt-in authorization. It must create a disposable pull request and observe a completed successful `CI` check bound to that pull request's current head; fail closed on missing credentials, unavailable OMP, unavailable exact-head CI evidence, or unresolved cleanup, and retain its evidence record. Inspecting a configured workflow without observing its check is insufficient.
- Test result collection→validation workspace handoff, coordinator crash before and after each provider effect, harness loss, source edit, base advance, head change, stale CI/review, review waiver, repair exhaustion, cancellation, terminal cleanup, and ambiguous PR creation against the composed runtime and command lifecycle. Run one real issue only after those tests pass and retain its evidence digest plus interruption-and-recovery narrative.

PER-PR GATES
Focused retained-workspace/progression/recovery tests, adversarial merge-ready script, then full uv gates. The opt-in live-provider preflight runs only after local fixtures pass and before the real Stage 1 gate. Live Stage 1 runs only after that preflight observes the required `CI` check on its disposable exact head.

REVIEW LOOP
Review from issue author, operator, and safety perspectives. Use mutation-test regression guard for any bug regression added during implementation. Never trigger external reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main. Topology: origin/main -> m08/corrective-workspace -> m08/corrective-progression -> m08/corrective-evidence. Merge root-to-leaf with fresh CI, clean branches/worktrees, and keep the separate gate PR unmerged if it is the workflow output fixture.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if the merged Stage 1 baseline and all three corrective implementation PRs merge; the live-provider preflight observes the required exact-head `CI` check; and a real issue produces a non-empty, current-head, evidence-complete merge-ready PR that remains open and unmerged, survives coordinator interruption without duplicate side effects, and records one terminal workspace cleanup or retained-reconciliation outcome. Any no-op pass, stale source/evidence, unreviewed medium/high change, missing live gate, failed preflight, or missing workspace outcome is NO-GO.

RETURN
Return implementation PRs, real gate issue/PR, interruption exercised, evidence bundle digest/summary, commands/results, release state, and verdict.
```

## M9 — Prove harness neutrality with Claude Code *(`v0.2.0` train — starts only after gate G1)*

```text
/goal
You are executing M9: prove the harness contract is provider-neutral using a Claude Code reference adapter alongside OMP.

GATE CHECK (required before any implementation work)
This milestone belongs to the v0.2.0 release train, which starts only after gate G1 passes: the Stage 1 gate (M8) is complete and the documented pilot (docs/pitch.md) returned `go`. Before proceeding, locate and report the recorded G1 pass evidence (pilot result, Stage 1 evidence-bundle digest). If it cannot be found, stop and report NO-GO — GATE G1 NOT PASSED instead of implementing.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M9
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§1–2, 4, 10–11
- Current Claude Code headless-mode and structured-output documentation; read the claude-api skill before opening implementation files.

TARGET
Run one normalized harness contract fixture through both OMP and Claude Code without adding provider-named domain/application fields or making either harness mandatory. Freeze the adapter contract against real Stage-1 usage friction (recorded in the M8 gate evidence and any post-launch pilot feedback), not against speculative future requirements.

RELEASE TARGET
v0.2.0; no release prep in this milestone.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M6 externally merged; gate G1 passed (see GATE CHECK).
- Harness absence is a diagnostic failure for that configured adapter, not a fallback trigger.
- Provider-specific capabilities stay in adapter capability discovery.
- Never hardcode model IDs; receive selected model metadata from configuration/harness output.

SCOPE
Claude Code availability/capability probe; headless start; structured/raw event normalization; cancellation; final status; artifacts; malformed output; fingerprint; optional installation metadata; shared fixture comparison; adapter contract documentation.

STACK DEPTH
4 PRs.

PLANNED STACK
1. feat(claude-code): probe capabilities and launch headless work
2. feat(claude-code): normalize events artifacts status and cancellation
3. test(harness): run shared OMP and Claude Code contract fixtures
4. docs(adapters): publish the provider-neutral harness contract

REQUIRED WORK
- Verify current CLI/SDK behavior rather than relying on memory.
- Use the exact M6 task envelope and normalized result types.
- Demonstrate common cancellation, timeout, malformed-event, and unavailable-harness semantics.
- Remove any OMP-specific contract field exposed by the comparison; migrate both adapters and callers cleanly. Where real Stage-1 usage surfaced a gap the original M6 contract did not anticipate, fix the contract against that evidence rather than against this second adapter's shape alone.

PER-PR GATES
Adapter-focused tests, shared fixture with each installed harness, import-boundary check, full uv gates.

REVIEW LOOP
Review for accidental lowest-common-denominator design, hidden provider fallbacks, model hardcoding, raw event leakage, and cancellation mismatch. Never solicit bot review.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main containing M6 or later. Topology: origin/main -> m09/harness-01 -> m09/harness-02 -> m09/harness-03 -> m09/harness-04. Validate current source/parent; merge root-to-leaf with fresh CI and clean.

RELEASE PREP
RELEASE PREP: not-required for this milestone. The `v0.2.0` release-preparation pass is M12b (DEVELOPMENT_PLAN.md §5), run after the full `v0.2.0` train — M9, M10, M11, M12 — externally merges.

FINAL VERDICT
GO only if four PRs merge and both installed harnesses pass the same behavioral contract with no provider-named inner fields and no fallback. If either reference adapter cannot be exercised, or gate G1 evidence cannot be found, return NO-GO with the missing prerequisite.

RETURN
Return adapter versions, shared capabilities/differences, fixture results, merged PRs, release state, and verdict.
```

## M10 — Enforce capability provenance and integrate Armory *(`v0.2.0` train — starts only after gate G1)*

```text
/goal
You are executing M10: implement capability locking/provenance and an optional Armory registry adapter.

GATE CHECK (required before any implementation work)
This milestone belongs to the v0.2.0 release train, which starts only after gate G1 passes (Stage 1 gate complete, documented pilot returned `go`). Before proceeding, locate and report the recorded G1 pass evidence. If it cannot be found, stop and report NO-GO — GATE G1 NOT PASSED instead of implementing.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M10
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§2, 4, 8, 10–11
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/overview.md §7

TARGET
Materialize immutable, policy-approved capabilities without making Armory a runtime dependency or treating content addressing as trust.

RELEASE TARGET
v0.2.0; no release prep in this milestone.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M6 and M4 externally merged; gate G1 passed (see GATE CHECK).
- Repository-local capabilities remain valid without a registry.
- Run-added/changed capabilities require interactive exact-digest human approval.
- Authenticated provenance means a verified signature chain against pinned identity, not TLS alone.

SCOPE
Capability metadata/lockfile; immutable materialization; local provenance; external signature verification; exact-digest approval; fingerprint binding; Armory package discovery/version resolution/materialization; license/provenance evidence; adversarial supply-chain tests.

STACK DEPTH
4 PRs.

PLANNED STACK
1. feat(capabilities): add locks immutable materialization and local provenance
2. feat(capabilities): enforce signature and exact-digest approval policy
3. feat(armory): implement optional capability registry adapter
4. test(capabilities): add malicious mutation and provenance gates

REQUIRED WORK
- Inspect Armory's current MCP/catalog surface before choosing integration.
- Keep the core usable with no Armory installation.
- Detect mutable-reference drift and adapter/capability version changes before resume.
- Test unsigned external assets, wrong signer, digest swap, license mismatch, run-introduced capability, and active-run mutation.

PER-PR GATES
Focused capability/Armory tests, adversarial script, full uv gates.

REVIEW LOOP
Review trust roots, signature verification, lock determinism, materialization path safety, license metadata, and self-approval. Do not trigger bots.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M6/M4. Topology: origin/main -> m10/capability-01 -> m10/capability-02 -> m10/capability-03 -> m10/capability-04. Validate, fresh-CI merge root-to-leaf, clean.

RELEASE PREP
RELEASE PREP: not-required for this milestone. The `v0.2.0` release-preparation pass is M12b, run after the full `v0.2.0` train externally merges.

FINAL VERDICT
GO only if four PRs merge; local operation works without Armory; Armory passes the same registry contract; every adversarial provenance/mutation case blocks; and exact approvals bind the executed digest. If gate G1 evidence cannot be found, return NO-GO with that missing prerequisite. Otherwise NO-GO on any adversarial failure.

RETURN
Return trust model, registry/provider versions, merged PRs, adversarial results, release state, and verdict.
```

## M11 — Implement plan ingestion, child runs, and stack topology *(`v0.2.0` train — starts only after gate G1)*

```text
/goal
You are executing M11: implement development-plan ingestion, dependency-safe child runs, parallel scheduling, and stacked PR topology.

GATE CHECK (required before any implementation work)
This milestone belongs to the v0.2.0 release train, which starts only after gate G1 passes (Stage 1 gate complete, documented pilot returned `go`). M11's own dependencies (M8, M5) may be satisfied earlier, but do not begin this milestone's stack before G1 evidence exists. Before proceeding, locate and report the recorded G1 pass evidence. If it cannot be found, stop and report NO-GO — GATE G1 NOT PASSED instead of implementing.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M11
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§5, 7, 12, 14, 17 (Stage 2 row)

TARGET
Turn a validated plan into linked child workflows without conflating work dependencies, run state, and git ancestry.

RELEASE TARGET
v0.2.0; no release prep in this milestone.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M8 and M5 externally merged; gate G1 passed (see GATE CHECK).
- Child runs have independent histories/evidence and explicit parent links.
- Independent milestones may run concurrently; dependencies are hard barriers.
- Stack topology is versioned data and must survive restart/rebase/reconciliation.

SCOPE
Plan schema/adapter; milestone normalization; dependency/cycle validation; child-run process manager; fan-out/join; concurrency budgets; separate work and branch DAGs; stack base/source/head metadata; partial resume; evidence projection.

STACK DEPTH
5 PRs.

PLANNED STACK
1. feat(plans): validate and normalize milestone dependency graphs
2. feat(engine): create linked child runs with fan-out and join
3. feat(scheduler): enforce dependencies and repository concurrency
4. feat(stacks): model branch topology evidence and reconciliation
5. test(plans): cover linear parallel diamond failure and resume cases

REQUIRED WORK
- Reject cycles/unresolved dependencies before child creation.
- Prove idempotent child creation after coordinator crash.
- Preserve completed siblings when one child blocks/fails.
- Model root-to-leaf merge readiness without performing merge/release yet.
- Stress a diamond plan with interruptions and concurrency.

PER-PR GATES
Focused plan/scheduler/stack tests, stress script, full uv gates.

REVIEW LOOP
Review graph semantics, idempotency, child correlation, repository limits, ancestry reconciliation, and evidence aggregation. Never trigger reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M8/M5. Topology: origin/main -> m11/plans-01 -> m11/plans-02 -> m11/plans-03 -> m11/plans-04 -> m11/plans-05. Validate and merge root-to-leaf with fresh CI, then clean.

RELEASE PREP
RELEASE PREP: not-required for this milestone. The `v0.2.0` release-preparation pass is M12b, run after the full `v0.2.0` train externally merges.

FINAL VERDICT
GO only if five PRs merge and linear, parallel, diamond, failed, cancelled, and resumed plans have correct child and branch topology with no duplicate child run. If gate G1 evidence cannot be found, return NO-GO with that missing prerequisite. Otherwise NO-GO on any topology defect.

RETURN
Return graph/stack fixtures, stress results, merged PRs, commands, release state, and verdict.
```

## M12 — Deliver plan to verified released version *(`v0.2.0` train)*

```text
/goal
You are executing M12: deliver Stage 2, from development plan to a verified released fixture version.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M12 and release rules
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§9–13, 17 (Stage 2 row)
- The current stacked-prs, ship-workflow, and release-provider guidance

TARGET
Merge a multi-milestone fixture root-to-leaf with fresh evidence, prepare version/changelog once, publish through fixed GitHub/PyPI brokers, and verify the actual destinations.

RELEASE TARGET
- Product train target: v0.2.0 (this milestone's stack merges into that train). Product version remains whatever pre-release identifier the v0.2.0 train uses. The v0.2.0 product release-preparation pass is M12b (DEVELOPMENT_PLAN.md §5), run after M9, M10, M11, and M12 externally merge; do not perform ad hoc product release preparation here.
- Stage 2 publishes a separate dedicated fixture distribution with unique fixture versions, independent of the product's own version.
- This milestone's own RELEASE PREP is not-required; the Stage 2 fixture publication below is a test-contract deliverable, not product release preparation.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M9, M10, and M11 externally merged.
- Publication credentials never enter worker workspaces, prompts, or arbitrary command nodes.
- Release preparation begins only after every included fixture milestone is externally merged.
- PyPI version immutability requires unique fixture versions and remediation, never deletion/reuse.

SCOPE
Merge policy/action; double-read current head and checks; root-to-leaf merge/retarget/cleanup; release-target validation; fixed version/changelog broker; fixture package build; fixed publication broker; GitHub Release/PyPI providers; ambiguous reconciliation; destination digest/version verification; Stage 2 evidence.

STACK DEPTH
6 PRs.

PLANNED STACK
1. feat(merge): enforce dependency order and fresh-head merge policy
2. feat(release): validate targets and broker version changelog updates
3. feat(release): build and verify fixture wheel and sdist
4. feat(publish): add GitHub Release and PyPI fixed brokers
5. feat(workflow): compose plan-to-release reconciliation and cleanup
6. test(stage2): execute multi-milestone dual-harness release gate

REQUIRED WORK
- Use fixed product-owned release/publish functions, not agent-authored shell.
- Verify current provider documentation and APIs before implementation.
- Fault-inject success-with-timeout at merge and publish boundaries and prove reconciliation using the shared fault-injection framework from M3/M5.
- Run shared harness contract through OMP and Claude Code.
- Verify clean installation and destination artifact digest for the fixture.

PER-PR GATES
Focused merge/release/publish tests, ambiguous-operation fault script, full uv gates. Live publication only after explicit human approval and against the dedicated fixture distribution.

REVIEW LOOP
Review credential confinement, target correctness, version uniqueness, changelog scope, merge freshness, PyPI irreversibility, and cleanup. Never trigger external reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main containing M9–M11. Topology: origin/main -> m12/stage2-01 -> m12/stage2-02 -> m12/stage2-03 -> m12/stage2-04 -> m12/stage2-05 -> m12/stage2-06. Merge root-to-leaf with fresh CI and clean. Keep fixture plan branches separate from implementation branches.

RELEASE PREP
RELEASE PREP: not-required for the product in this milestone. Fixture release preparation is part of the Stage 2 test contract only and must use its separate identity/version namespace.

FINAL VERDICT
GO only if all six PRs merge and a real multi-milestone fixture reaches a destination-verified GitHub/PyPI release with correct merge order, exact-head evidence, one release-prep pass, dual-harness contract proof, and no duplicate external effect. Missing publication approval/credentials or destination verification is NO-GO.

RETURN
Return implementation PRs, fixture PR/release/PyPI URLs, merge order, artifact hashes, failure injection results, and verdict.
```

## M12b — Prepare, publish, and verify v0.2.0

```text
/goal
You are executing M12b: perform the single v0.2.0 release-preparation pass, publish to PyPI and GitHub Releases, and verify clean consumers and destinations.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M12b, §§4 and 6 release rules
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§9, 11–13, 17–18
- Current repository release conventions, PyPI metadata, GitHub release configuration, and current official uv/GitHub CLI documentation

TARGET
Publish exactly the already-implemented M9, M10, M11, and M12 additions as v0.2.0, layered on the already-published v0.1.0. Add no features, provider expansions, schema changes, or contract redesign. This release does not include M13 (v0.3.0 train) or M14b/M15 (gate-deferred) — do not scope-creep them into this release-preparation pass.

RELEASE TARGET
v0.2.0. This milestone owns the only product version update, CHANGELOG.md entry, tag, PyPI publication, and GitHub Release for this train.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M9, M10, M11, and M12 must be externally merged to origin/main, on top of the already-published v0.1.0. The Stage 2 evidence bundle must pass. Do not wait for M13, M14b, or M15 — they belong to other trains or are gate-deferred.
- Obtain interactive human approval before irreversible PyPI/GitHub publication.
- PyPI versions and published tags are immutable; never delete/reuse/rewrite v0.2.0 after publication.
- Use uv only for Python environment/build/publish operations.
- No feature work in release prep.

SCOPE
Canonical version update 0.1.0 -> 0.2.0; CHANGELOG.md entry; release notes; compatibility/known-limit statement (must state that Stage 3–4 and self-improvement are not yet implemented and Stage 4 is gate-deferred); build; wheel/sdist checks; dependency/SBOM manifest; hashes; clean install on macOS/Ubuntu; CLI smoke; release-prep PRs; annotated tag; uv publish; GitHub Release; destination verification; post-publication evidence.

STACK DEPTH
2 PRs.

PLANNED STACK
1. build(release): prepare v0.2.0 version changelog and artifacts
2. docs(release): finalize compatibility and release notes

REQUIRED WORK
- Re-read the actual repository release conventions and package metadata before editing.
- Confirm only canonical version/changelog/release documentation changes are needed; fix release-tooling defects in a separately justified pre-release PR before restarting M12b.
- Build once from the final intended commit; verify artifact metadata and hashes.
- Clean-install the wheel/sdist on macOS and Ubuntu and execute version, doctor, and the cumulative Stage 1+2 gate (`full_system_gate.py --stages 1,2`) smoke.
- Create the v0.2.0 tag only after both release-prep PRs merge and exact main commit is green.
- After human approval, publish with uv and create the GitHub Release; then query both destinations and clean-install from PyPI.
- State in the release notes that this release adds second-harness neutrality (Claude Code), capability provenance, and Stage 2 (plan to released version) on top of v0.1.0: Stage 3 ships in v0.3.0, and Stage 4 is gate-deferred behind G4 with no committed date.

PER-PR GATES
Run uv run python scripts/release_gate.py --version 0.2.0, uv build, uvx twine check dist/*, artifact hash verification, clean-install smoke, full lint/type/test/E2E gates, and CI. Never accept a stale artifact built from another commit.

REVIEW LOOP
Perform pre-landing, security, package metadata, changelog, and numeric-claim review. Confirm release notes match checked evidence and make no hosted/container/Windows/production-security/self-improvement claims. Address organic reviewer feedback only; never request bot review.

MERGE DISCIPLINE
Read stacked-prs and the applicable release-publish skill before git topology or publication. Base origin/main with M9, M10, M11, M12. Topology: origin/main -> m12b/release-01 -> m12b/release-02. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, clean branches, then verify origin/main exact commit before tagging. Never publish from a feature branch.

RELEASE PREP
RELEASE PREP: pending -> ready only when both PRs are externally merged, release_gate passes on origin/main, artifacts are built from that commit, and human approval is recorded. After publication and destination verification, state becomes completed. Any failure is NO-GO; do not partially relabel it ready.

FINAL VERDICT
GO only if v0.2.0 is tagged from the verified main commit, PyPI and GitHub Release contain the intended artifacts/hashes, a clean PyPI consumer passes CLI/Stage 1+2 workflow smoke, all branches are clean, release notes correctly scope this release and disclose the Stage 4 gate deferral, and release evidence is recorded. Before publication approval or with any mismatch, return NO-GO and preserve the unpublished or partially published state for remediation.

RETURN
Return release-prep PRs, exact commit/tag, changelog/release-note paths, build artifact names/hashes, macOS/Ubuntu clean-install results, PyPI and GitHub URLs, publication approval record, RELEASE PREP completed or NO-GO state, and final verdict.
```

## M13 — Deliver incident to hotfix and actual rollback *(`v0.3.0` train)*

```text
/goal
You are executing M13: deliver Stage 3, from a production-style incident to a verified hotfix and actual rollback on the controlled local service.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M13
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§8, 11–13, 15, 17 (Stage 3 row)

TARGET
Prove urgent routing and authority boundaries without touching real production: reproduce a controlled fault, implement a minimal hotfix, deploy through fixed broker code, observe, trigger rollback, and verify prior revision restoration.

RELEASE TARGET
v0.3.0. This milestone's own product release-preparation is not-required; the `v0.3.0` release-preparation pass is M13b (DEVELOPMENT_PLAN.md §5), run after M13 externally merges and `v0.2.0` (M12b) is already published.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M12 and M8 externally merged.
- The controlled deployment target is a local versioned HTTP service fixture.
- No agent receives deployment/publication credentials or unrestricted host authority.
- Deployment and rollback require separate explicit policy decisions.
- A simulated rollback is insufficient; change the live fixture revision and observe restoration.

SCOPE
Incident domain intake; severity/authority; containment; release-lineage selection; reproduction; hotfix worktree; minimal repair; non-vacuous regression evidence; emergency PR; fixed local deployment and rollback brokers; health/observation thresholds; follow-up work; Stage 3 evidence.

STACK DEPTH
6 PRs.

PLANNED STACK
1. feat(incidents): ingest classify and bind release lineage
2. feat(incidents): reproduce faults and separate containment
3. feat(workflow): implement validate and review minimal hotfixes
4. feat(deployment): add fixed local service deploy and observe broker
5. feat(deployment): add rollback authority reconciliation and evidence
6. test(stage3): execute mutation guard deploy failure and restoration gate

REQUIRED WORK
- Build a real version-reporting service fixture with an injectible defect and health signal.
- Make regression evidence fail against the unfixed revision and pass after repair where feasible.
- Fault-inject deploy ambiguity, observation timeout, rollback ambiguity, coordinator crash, and expired credential reference, reusing the shared fault-injection framework from M3/M5.
- Create separate follow-up work rather than expanding emergency scope.

PER-PR GATES
Focused incident/deployment tests, mutation test in isolated scratch worktree, Stage 3 script, full uv gates.

REVIEW LOOP
Use security, silent-failure, and mutation-regression review. Inspect authority, lineage, rollback preconditions, observation windows, credential lifetime, and emergency scope. Never solicit bot review.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M12/M8. Topology: origin/main -> m13/stage3-01 -> m13/stage3-02 -> m13/stage3-03 -> m13/stage3-04 -> m13/stage3-05 -> m13/stage3-06. Validate, fresh-CI merge root-to-leaf, clean processes/worktrees/branches.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if six PRs merge and the controlled live service is deployed to the hotfix, observed, deliberately driven across the rollback threshold, rolled back, and observed at the prior revision with all authority records. Any simulation-only rollback, vacuous regression test, agent-held credential, or missing live gate is NO-GO.

RETURN
Return merged PRs, incident/release lineage, mutation result, deploy/rollback revisions, observation evidence, commands, and verdict.
```

## M13b — Prepare, publish, and verify v0.3.0

```text
/goal
You are executing M13b: perform the single v0.3.0 release-preparation pass, publish to PyPI and GitHub Releases, and verify clean consumers and destinations.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M13b, §§4 and 6 release rules
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§9, 11–13, 17–18
- Current repository release conventions, PyPI metadata, GitHub release configuration, and current official uv/GitHub CLI documentation

TARGET
Publish exactly the already-implemented M13 additions as v0.3.0, layered on the already-published v0.1.0 and v0.2.0. Add no features, provider expansions, schema changes, or contract redesign. This release does not include M14b/M15 (gate-deferred) — do not scope-creep them into this release-preparation pass.

RELEASE TARGET
v0.3.0. This milestone owns the only product version update, CHANGELOG.md entry, tag, PyPI publication, and GitHub Release for this train.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M13 must be externally merged to origin/main, on top of the already-published v0.2.0 (M12b externally merged and published). The Stage 3 evidence bundle must pass. Do not wait for M14b or M15 — they are gate-deferred.
- Obtain interactive human approval before irreversible PyPI/GitHub publication.
- PyPI versions and published tags are immutable; never delete/reuse/rewrite v0.3.0 after publication.
- Use uv only for Python environment/build/publish operations.
- No feature work in release prep.

SCOPE
Canonical version update 0.2.0 -> 0.3.0; CHANGELOG.md entry; release notes; compatibility/known-limit statement (must state that self-improvement is not yet implemented and Stage 4 is gate-deferred); build; wheel/sdist checks; dependency/SBOM manifest; hashes; clean install on macOS/Ubuntu; CLI smoke; release-prep PRs; annotated tag; uv publish; GitHub Release; destination verification; post-publication evidence.

STACK DEPTH
2 PRs.

PLANNED STACK
1. build(release): prepare v0.3.0 version changelog and artifacts
2. docs(release): finalize compatibility and release notes

REQUIRED WORK
- Re-read the actual repository release conventions and package metadata before editing.
- Confirm only canonical version/changelog/release documentation changes are needed; fix release-tooling defects in a separately justified pre-release PR before restarting M13b.
- Build once from the final intended commit; verify artifact metadata and hashes.
- Clean-install the wheel/sdist on macOS and Ubuntu and execute version, doctor, and the cumulative Stage 1+2+3 gate (`full_system_gate.py --stages 1,2,3`) smoke.
- Create the v0.3.0 tag only after both release-prep PRs merge and exact main commit is green.
- After human approval, publish with uv and create the GitHub Release; then query both destinations and clean-install from PyPI.
- State in the release notes that this release adds Stage 3 (incident to hotfix and rollback) on top of v0.1.0/v0.2.0, and that Stage 4 is gate-deferred behind G4 with no committed date.

PER-PR GATES
Run uv run python scripts/release_gate.py --version 0.3.0, uv build, uvx twine check dist/*, artifact hash verification, clean-install smoke, full lint/type/test/E2E gates, and CI. Never accept a stale artifact built from another commit.

REVIEW LOOP
Perform pre-landing, security, package metadata, changelog, and numeric-claim review. Confirm release notes match checked evidence and make no hosted/container/Windows/production-security/self-improvement claims. Address organic reviewer feedback only; never request bot review.

MERGE DISCIPLINE
Read stacked-prs and the applicable release-publish skill before git topology or publication. Base origin/main with M13, on top of the published v0.2.0 (M12b). Topology: origin/main -> m13b/release-01 -> m13b/release-02. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, clean branches, then verify origin/main exact commit before tagging. Never publish from a feature branch.

RELEASE PREP
RELEASE PREP: pending -> ready only when both PRs are externally merged, release_gate passes on origin/main, artifacts are built from that commit, and human approval is recorded. After publication and destination verification, state becomes completed. Any failure is NO-GO; do not partially relabel it ready.

FINAL VERDICT
GO only if v0.3.0 is tagged from the verified main commit, PyPI and GitHub Release contain the intended artifacts/hashes, a clean PyPI consumer passes CLI/Stage 1+2+3 workflow smoke, all branches are clean, release notes correctly scope this release and disclose the Stage 4 gate deferral, and release evidence is recorded. Before publication approval or with any mismatch, return NO-GO and preserve the unpublished or partially published state for remediation.

RETURN
Return release-prep PRs, exact commit/tag, changelog/release-note paths, build artifact names/hashes, macOS/Ubuntu clean-install results, PyPI and GitHub URLs, publication approval record, RELEASE PREP completed or NO-GO state, and final verdict.
```

## M14a — Build outcome schema and raw observation capture *(`v0.1.0` train)*

```text
/goal
You are executing M14a: build the outcome-observation schema and raw capture pipeline so Stage 1 runs emit immutable, versioned outcome data from the first release. This milestone is deliberately narrow — it does not build cohorts, replay, comparison, or candidate evaluation; those are M14b, gate-deferred behind gate G4 (DEVELOPMENT_PLAN.md §8). Do not implement M14b/M15 scope here even if it seems like a small addition.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M14a
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§5, 9, 13–14, 17 (outcome and observation portions only — ignore §12.4 cohort/replay/candidate content; that belongs to M14b/M15)

TARGET
Make every Stage 1 run emit immutable, versioned raw outcome observations (merge, reopen, escaped defect; indeterminate where unobserved) without retrofitting capture onto un-instrumented history later.

RELEASE TARGET
v0.1.0; no release prep.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M8 externally merged. Do not wait for M12/M13 — this milestone's outcome adapters cover Stage 1 subjects only; Stage 2/3 outcome adapters are added when those trains ship, without a schema migration if the schema is versioned correctly here.
- Raw observations are immutable. Derivations/formulas are versioned separately from raw data.
- Unavailable outcomes are indeterminate, never treated as favorable.
- No cohort registry, no replay environment, no comparison logic, no candidate-facing API in this milestone.

SCOPE
Outcome record schema; outcome adapters for Stage 1 subjects (merge, reopen, escaped defect); observation windows; indeterminate outcome; raw metric observations; versioned derivations; outcome-capture completeness metric; intervention and failure queries; CLI inspect commands.

STACK DEPTH
3 PRs.

PLANNED STACK
1. feat(outcomes): add the versioned raw-observation schema and Stage-1 adapters
2. feat(outcomes): compute outcome-capture completeness and derivation versioning
3. test(outcomes): add suppression and delayed-attribution adversarial gates

REQUIRED WORK
- Import actual evidence references from Stage 1 (M8) runs.
- Represent capture completeness and delayed outcomes explicitly.
- Prove suppressing attribution worsens the completeness metric rather than improving any downstream score — there is no downstream score in this milestone, but the metric itself must not be gameable by omission.
- Verify a dogfooding run (using this repository's own Stage-1 workflow to build later milestones) produces a queryable observation.
- Design the schema so M14b's later cohort/replay work needs no migration against this milestone's data — version the schema explicitly and document the compatibility contract.

PER-PR GATES
Focused outcome/metric tests, adversarial script, full uv gates.

REVIEW LOOP
Review causal attribution, missing-data semantics, formula-versioning discipline, and query performance. Never trigger reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M8. Topology: origin/main -> m14a/outcomes-01 -> m14a/outcomes-02 -> m14a/outcomes-03. Validate and merge root-to-leaf with fresh CI, clean.

RELEASE PREP
RELEASE PREP: not-required.

FINAL VERDICT
GO only if three PRs merge; missing outcomes remain indeterminate; suppression lowers the completeness metric; raw observations survive a formula-version change; and no cohort/replay/candidate scope was implemented. Otherwise NO-GO.

RETURN
Return outcome schema version, completeness-metric definition, adversarial results, merged PRs, release state, and verdict.
```

## M14b — Build cohort registry, replay, and comparison foundation *(gate-deferred — do not execute before gate G4 passes)*

```text
/goal
You are executing M14b: build the comparable-cohort registry and side-effect-free replay environment on top of the M14a observation corpus.

GATE CHECK (mandatory — perform before any other action)
M14b is blocked until M24 is externally merged and `enginery gate status --gate G4 --json` reports `overall: "pass"` from a persistent ledger. The report must prove, not merely assert:
1. A verified-complete classified cohort at the registered volume floor, spanning at least two supported work kinds and two supported risk classes.
2. Recorded human interventions with reasons at the registered floor.
3. Outcome-capture completeness at or above the registered floor.
4. An immutable recurring-deficiency finding that binds at least two eligible runs and durable supporting evidence, represented by a merged GitHub evidence PR with two distinct configured numeric GitHub identities approving its exact reviewed head; neither reviewer may be the PR author or cited-evidence producer.
5. At least two repositories in that same classified, verified-complete cohort.
6. At least two registered human AuthorityPrincipals mapped to immutable GitHub numeric identities.

If M24 is not merged, the report is not `pass`, or any record is missing, stale, unclassified, unverifiable, or self-approved, STOP and report exactly:
NO-GO — GATE G4 NOT PASSED — <which condition(s) failed>
Do not proceed to TARGET, SCOPE, or PLANNED STACK below in that case.

SOURCE OF TRUTH (read only after the gate check passes)
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M14b
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§5, 12.4, 13, 17

TARGET
Turn the accumulated M14a raw-observation corpus into registered comparable cohorts and a replay/shadow environment suitable for M15's baseline-versus-candidate evaluation, without candidate generation, canary, or promotion logic.

RELEASE TARGET
Assigned when gate G4 passes (unversioned until then, per DEVELOPMENT_PLAN.md §4).

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M14a externally merged; gate G4 passed (see GATE CHECK).
- Candidate/proposer cannot define cohort filters or access held-out selection — there is no candidate concept yet in this milestone, but the registry must be built so M15 cannot violate this later.
- Replay/shadow mode cannot invoke real side-effect adapters under any configuration.
- Corpus diversity from the gate check must be reflected in the registered cohorts; do not silently collapse a two-repository corpus into a single cohort.

SCOPE
Comparable cohort schema; fixed or independent cohort registry; replay environment; side-effect-disabled providers; deterministic comparison; CLI compare skeleton.

STACK DEPTH
3 PRs.

PLANNED STACK
1. feat(evaluation): register independent comparable cohorts from the M14a corpus
2. feat(replay): execute side-effect-free historical workflows
3. test(evaluation): add cohort-comparability and replay-isolation gates

REQUIRED WORK
- Build cohorts from the real corpus verified in the gate check, not from synthetic fixtures alone.
- Reject incompatible work/risk populations in comparability checks.
- Prove replay cannot resolve production/publication/deployment brokers under any configuration.
- Document why a single-repository-only corpus would have failed comparability, using the actual multi-source corpus as the counter-example.

PER-PR GATES
Focused cohort/replay tests, adversarial script, full uv gates.

REVIEW LOOP
Review cohort comparability, replay isolation, and query performance against the real (not synthetic-only) corpus. Never trigger reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M14a. Topology: origin/main -> m14b/eval-01 -> m14b/eval-02 -> m14b/eval-03. Validate and merge root-to-leaf with fresh CI, clean.

RELEASE PREP
RELEASE PREP: not-required; train unassigned until G4 passage is recorded (see RELEASE TARGET).

FINAL VERDICT
GO only if the gate check passed with recorded evidence, three PRs merge, cohort selection is independent and reproducible against the real corpus, and replay has zero real side effects under test. NO-GO if the gate check fails, or on any comparability/isolation defect.

RETURN
Return the G4 gate evidence located, cohort registry contents, replay isolation proof, merged PRs, release state, and verdict.
```

## M15 — Deliver governed factory self-improvement *(gate-deferred — do not execute before gate G4 passes)*

```text
/goal
You are executing M15: deliver Stage 4 governed factory self-improvement with independent evaluation, canary, promotion, retention, and rollback.

GATE CHECK (mandatory — perform before any other action)
M15 is blocked by gate G4 (DEVELOPMENT_PLAN.md §8), the same gate as M14b, plus one M15-specific precondition. Before proceeding:
1. Confirm M14b is externally merged and its own gate-check evidence is still valid (cohorts and replay built from a real, diverse corpus).
2. Confirm at least two registered human AuthorityPrincipals mapped to immutable GitHub numeric identities exist in this deployment. Canary approval and promotion approval are dual-human separations (docs/design.md §6, §10.4) that a single-operator deployment cannot satisfy — this is a declared limit, not a check to work around. If only one human principal is registered, STOP and report exactly:
NO-GO — GATE G4 NOT PASSED — single-operator deployment cannot satisfy dual-human separation for canary/promotion
Do not implement a workaround (for example, treating the same person under two role labels as separation) — the design explicitly rejects that.
3. Confirm at least one recurring, evidence-backed workflow deficiency is documented as the real candidate's diagnosis. Do not fabricate a favorable candidate to exercise the milestone.
If any condition is unmet, STOP and report NO-GO — GATE G4 NOT PASSED — <which condition failed> instead of implementing.

SOURCE OF TRUTH (read only after the gate check passes)
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M15
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§4–5, 8 (hard rules 8–10), 12.4, 13, 17 (Stage 4 row)
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §8 (dual-human separation) and §17 (Stage 4 gate deferral)

TARGET
Evaluate one real candidate factory change using earlier-run evidence, reject validation weakening and metric gaming, require separate human canary/promotion decisions from two distinct registered principals, and produce a real promote/retain/reject plus rollback record.

RELEASE TARGET
Assigned when gate G4 passes (unversioned until then).

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M14b, M10, and M4 externally merged; gate G4 and the M15-specific preconditions passed (see GATE CHECK).
- Candidate cannot mutate itself, evaluator, policy, held-out inputs, or active workflow in place.
- Proposer/candidate cannot author or inspect held-out selection before evaluation completes.
- Canary and promotion approvers are two separate interactive humans, distinct from each other and from the proposer/requesting run.
- Authority-affecting candidates never control production-authoritative actions during canary.
- Adversarial-gate acceptance is scoped to an enumerated, versioned set of gaming families (validation weakening, cohort bias, outcome suppression, case omission, overfitting); do not claim or imply the gate rejects "every" possible gaming strategy — extend the enumerated set as new families are identified.

SCOPE
FactoryChange state machine; hypothesis/evidence link; candidate asset lock; held-out family; baseline/candidate replay; hard constraints; independent evaluator; the enumerated adversarial gaming families; factory-change PR; canary bounds; retained state; promotion pointer; rollback; compare/evaluate/propose/canary/promote/rollback CLI.

STACK DEPTH
6 PRs.

PLANNED STACK
1. feat(factory): add candidate hypothesis lock and lifecycle
2. feat(evaluation): compare baseline and candidate on held-out cohorts
3. test(evaluation): reject weakening bias suppression omission and overfit (enumerated families)
4. feat(factory): create reviewable factory-change PR and authority records
5. feat(canary): add bounded shadow canary retain promote and rollback
6. test(stage4): execute real candidate and randomized held-out gate

REQUIRED WORK
- Choose a real bounded candidate supported by the recurring, evidence-backed deficiency confirmed in the gate check; do not invent a fabricated win.
- Keep the evaluator/version/held-out digest independent and immutable during the run.
- Ensure every exclusion is symmetric and reviewed.
- Test each enumerated gaming family with randomized fixtures and unseen variants; document the enumerated set's version.
- Demonstrate active-version pointer rollback and preserved candidate history.
- Verify the two canary/promotion approvers are genuinely distinct registered principals, not the same operator under two labels.

PER-PR GATES
Focused factory/evaluation/canary tests, adversarial randomized gate against the enumerated family set, full uv gates. No candidate can satisfy its own test oracle.

REVIEW LOOP
Use independent safety review and a separate evaluation-method review. Inspect leakage, cohort bias, proxy metrics, approval identity (confirm true dual-human separation), canary authority, rollback, and retained-state reachability. Never trigger reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M14b/M10/M4. Topology: origin/main -> m15/stage4-01 -> m15/stage4-02 -> m15/stage4-03 -> m15/stage4-04 -> m15/stage4-05 -> m15/stage4-06. Validate and merge root-to-leaf with fresh CI. The candidate factory-change PR is evidence, not part of this implementation stack unless its own gate passes.

RELEASE PREP
RELEASE PREP: not-required; train unassigned until G4 passage is recorded.

FINAL VERDICT
GO only if the gate check (including the dual-human precondition) passed with recorded evidence, six implementation PRs merge, one real candidate is independently evaluated on hidden/randomized cases against the enumerated gaming-family set, receives separate human authority decisions from two distinct principals, completes a bounded safe canary, reaches promoted/retained/rejected, and has an executed rollback record. Any leakage, self-approval, single-principal-as-two-roles approval, validation weakening, production-authoritative canary, or canned-fixture gate is NO-GO.

RETURN
Return the G4 gate evidence located, the two distinct approver identities, implementation PRs, candidate/evaluator/cohort digests, scorecard, adversarial results (with the enumerated family-set version), canary decision/limits, final state, rollback evidence, release state, and verdict.
```

## M16 — Complete operator documentation and prove cumulative Stage-1 behavior *(`v0.1.0` train)*

```text
/goal
You are executing M16: complete operator/provider documentation and prove cumulative Stage-1 recovery behavior before v0.1.0 release preparation. This milestone no longer includes an engineered sage-dev importer — that scope was descoped 2026-07-14 to a manual, human-executed migration guide (docs/design.md §2.2; DEVELOPMENT_PLAN.md M16). Do not build an automated `.sage/tickets` importer here.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M16
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§2, 14–17
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/overview.md §§5, 8
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §8 (single-operator authority model — document its Stage-4 dual-human limit explicitly) and §9 (documented TOCTOU residual window)
- Current sage-dev .sage/tickets and dependency semantics from https://github.com/Mathews-Tom/sage-dev, for writing the manual migration guide only

TARGET
Preserve useful historical sage-dev work data through a documented manual procedure (not engineered automation), and establish release-ready operational evidence and documentation scoped to Stage 1 only.

RELEASE TARGET
v0.1.0. Keep 0.0.0.dev0 and do not create the v0.1.0 changelog/tag/publication yet.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M3, M8, and M14a externally merged. Do not wait for M12, M13, or M15 — those are separate release trains (v0.2.0, v0.3.0) or gate-deferred (M14b/M15) and are out of scope for this milestone's cumulative gate.
- The manual migration guide must be executable by hand against a real sage-dev fixture without ledger pollution; it replaces the previously planned automated importer, not the goal of preserving sage-dev data.
- Do not add /sage.* aliases, shell compatibility, or an in-place sage-dev upgrade path.
- Documentation states worktree security limits, credential broker boundaries, provider prerequisites, the single-operator authority model (including its Stage-4 dual-human limit), the documented merge-ready TOCTOU residual window, recovery semantics, and unsupported Windows behavior.

SCOPE
Operator install/config/doctor/recovery/backup/security documentation; adapter authoring documentation; Armory relationship; example workflows; Stage-1-only cumulative restart/replay gate; performance baseline; a documented, human-executable manual sage-dev migration guide (with a backup step); release-readiness report.

STACK DEPTH
4 PRs.

PLANNED STACK
1. docs(operators): document install policy evidence recovery and security, including the single-operator authority model and TOCTOU disclosure
2. docs(migration): write the manual sage-dev migration guide with a required backup step
3. docs(adapters): document contracts examples and Armory relationship
4. test(system): run Stage-1 cumulative restart recovery and performance gates

REQUIRED WORK
- Inspect real sage-dev samples and repository semantics before writing the manual guide; verify a human can follow it against a real fixture without ledger pollution, using a backup-first step.
- Run the Stage 1 workflow with process restart/replay between runs and index its evidence bundle. Do not attempt a Stage 2–4 cumulative gate here — those are their own trains' responsibility.
- Measure and record local performance bounds for the Stage-1 path only; do not claim unmeasured performance.
- Validate every Mermaid diagram touched by this milestone's documentation with the real engine or a Mermaid validator.
- State explicitly in the documentation that M14b/M15 are gate-deferred and not part of this release.

PER-PR GATES
Focused docs/system tests, Mermaid validation for changed docs, full uv gates. Documentation commands must be executed or generated from tested CLI help.

REVIEW LOOP
Review migration-guide clarity and safety (not automation correctness — there is no automation), restore behavior, command accuracy, security claims, stale prose, example reproducibility, and Stage-1 cumulative evidence. Never trigger external reviewers.

MERGE DISCIPLINE
Read stacked-prs. Base origin/main with M3, M8, M14a. Topology: origin/main -> m16/stabilize-01 -> m16/stabilize-02 -> m16/stabilize-03 -> m16/stabilize-04. Validate, merge root-to-leaf with fresh CI, clean branches/worktrees and any fixture processes.

RELEASE PREP
RELEASE PREP: pending. Do not begin it in M16. M17 begins only after M1–M8, M14a, and M16 are all externally merged and the Stage-1 cumulative readiness report passes.

FINAL VERDICT
GO only if four PRs merge; the manual migration guide is verified human-executable and non-mutating without its backup step; all documented commands are verified; all Mermaid blocks parse; the Stage-1 gate passes cumulatively across restart; performance claims match measurements; and the documentation correctly states the single-operator authority limit and the TOCTOU residual window. Otherwise NO-GO.

RETURN
Return merged PRs, docs paths, the Stage-1 evidence digest, recovery/performance results, the manual migration guide location, release target, RELEASE PREP pending, and verdict.
```

## M17 — Prepare, publish, and verify v0.1.0 *(`v0.1.0` train)*

```text
/goal
You are executing M17: perform the single v0.1.0 release-preparation pass, publish to PyPI and GitHub Releases, and verify clean consumers and destinations.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M17, §§4 and 6 release rules
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§9, 11–13, 17–18
- Current repository release conventions, PyPI metadata, GitHub release configuration, and current official uv/GitHub CLI documentation

TARGET
Publish exactly the already-implemented M1–M8, M14a, and M16 product as v0.1.0. Add no features, provider expansions, schema changes, or contract redesign. This release does not include M9–M13 (later trains) or M14b/M15 (gate-deferred) — do not scope-creep them into this release-preparation pass.

RELEASE TARGET
v0.1.0. This milestone owns the only product version update, CHANGELOG.md entry, tag, PyPI publication, and GitHub Release for this train.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this `/goal` block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`. After approved replanning, update `DEVELOPMENT_PLAN.md` and regenerate this file. Reassessment may strengthen contracts and verification; it must not weaken acceptance, erase safety evidence, widen release scope, or bypass gates.
- M1–M8, M14a, and M16 must be externally merged to origin/main. The Stage 1 evidence bundle and the M16 cumulative readiness report must pass. Do not wait for M9–M13, M14b, or M15 — they belong to other trains or are gate-deferred.
- Obtain interactive human approval before irreversible PyPI/GitHub publication.
- PyPI versions and published tags are immutable; never delete/reuse/rewrite v0.1.0 after publication.
- Use uv only for Python environment/build/publish operations.
- No feature work in release prep.

SCOPE
Canonical version update 0.0.0.dev0 -> 0.1.0; CHANGELOG.md; release notes; compatibility/known-limit statement (must state that Stage 2–4 and self-improvement are not yet implemented and Stage 4 is gate-deferred); migration notice (points to the manual guide, not an automated importer); build; wheel/sdist checks; dependency/SBOM manifest; hashes; clean install on macOS/Ubuntu; CLI smoke; release-prep PRs; annotated tag; uv publish; GitHub Release; destination verification; post-publication evidence.

STACK DEPTH
2 PRs.

PLANNED STACK
1. build(release): prepare v0.1.0 version changelog and artifacts
2. docs(release): finalize compatibility migration and release notes

REQUIRED WORK
- Re-read the actual repository release conventions and package metadata before editing.
- Confirm only canonical version/changelog/release documentation changes are needed; fix release-tooling defects in a separately justified pre-release PR before restarting M17.
- Build once from the final intended commit; verify artifact metadata and hashes.
- Clean-install the wheel/sdist on macOS and Ubuntu and execute version, doctor, and one local deterministic Stage-1 workflow smoke.
- Create the v0.1.0 tag only after both release-prep PRs merge and exact main commit is green.
- After human approval, publish with uv and create the GitHub Release; then query both destinations and clean-install from PyPI.
- State in the release notes that this is a Stage-1-only release: Stage 2/3 ship in v0.2.0/v0.3.0, and Stage 4 is gate-deferred behind G4 with no committed date.

PER-PR GATES
Run uv run python scripts/release_gate.py --version 0.1.0, uv build, uvx twine check dist/*, artifact hash verification, clean-install smoke, full lint/type/test/E2E gates, and CI. Never accept a stale artifact built from another commit.

REVIEW LOOP
Perform pre-landing, security, package metadata, changelog, and numeric-claim review. Confirm release notes match checked evidence and make no hosted/container/Windows/production-security/self-improvement claims. Address organic reviewer feedback only; never request bot review.

MERGE DISCIPLINE
Read stacked-prs and the applicable release-publish skill before git topology or publication. Base origin/main with M1–M8, M14a, M16. Topology: origin/main -> m17/release-01 -> m17/release-02. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, clean branches, then verify origin/main exact commit before tagging. Never publish from a feature branch.

RELEASE PREP
RELEASE PREP: pending -> ready only when both PRs are externally merged, release_gate passes on origin/main, artifacts are built from that commit, and human approval is recorded. After publication and destination verification, state becomes completed. Any failure is NO-GO; do not partially relabel it ready.

FINAL VERDICT
GO only if v0.1.0 is tagged from the verified main commit, PyPI and GitHub Release contain the intended artifacts/hashes, a clean PyPI consumer passes CLI/Stage-1-workflow smoke, all branches are clean, release notes correctly scope Stage 1-only and disclose the Stage 4 gate deferral, and release evidence is recorded. Before publication approval or with any mismatch, return NO-GO and preserve the unpublished or partially published state for remediation.

RETURN
Return release-prep PRs, exact commit/tag, changelog/release-note paths, build artifact names/hashes, macOS/Ubuntu clean-install results, PyPI and GitHub URLs, publication approval record, RELEASE PREP completed or NO-GO state, and final verdict.
```

## M18 — Report gate G4 readiness against the registered conditions

```text
/goal
You are executing M18: build a deterministic gate-G4 readiness-reporting command against the conditions already registered in DEVELOPMENT_PLAN.md §8. This milestone does not attempt to satisfy G4 itself — corpus diversity and a second registered human principal are operational actions no code deliverable can manufacture.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M18, §§2, 4, 8
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/FUTURE_ENHANCEMENTS.md §3
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §13

TARGET
Add a read-only CLI command reporting the current state of every gate-G4 condition using only durably captured M14a outcome/intervention data plus a registered-principal and repository-diversity count. Do not build any mechanism that itself creates corpus diversity or registers a human principal, and do not begin any Stage 4 design work.

RELEASE TARGET
`v0.4.0` per DEVELOPMENT_PLAN.md §2's 2026-08-04 DECISION and §4, assigned after this milestone had already merged. This milestone still performs no version bump, changelog entry, tag, or publication — M19b owns all of them for this train. Historical note: this block originally read `> GAP: unresolved`, and M18 was implemented and merged under that framing.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Compare the current default branch, finalized product documents, dependency evidence, active decision gates, and prior corrective evidence against this milestone. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`, not an implementation task.
- Do not proceed even after `GO` until a human approves the reassessment entry. `REPLAN REQUIRED` stops this block with `NO-GO — MILESTONE REASSESSMENT APPROVAL REQUIRED`.
- M14a must be externally merged (it is, as of `v0.1.0`).
- Every condition this command cannot measure from already-captured data must report `unmeasured`, never `pass`. Never imply G4 has passed when it has not.
- No Stage 4 design-detail work of any kind — that prohibition is independent of, and unaffected by, this milestone.

SCOPE
A `gate status --gate G4` (or equivalent) CLI command; a registered-floor configuration file; reuse of M14a's existing outcome-capture/completeness projection; a registered-principal count; a repository-diversity count derived from configured repository targets already recorded in the ledger.

STACK DEPTH
3 PRs.

PLANNED STACK
1. feat(gate): add the registered G4-floor configuration and principal/repository counters
2. feat(cli): add the gate status command over existing outcome/intervention projections
3. test(gate): add zero/one/two-principal and one/two-repository fixtures proving fail-closed unmeasured reporting

REQUIRED WORK
- Re-read the current M14a outcome/completeness projection and CLI conventions before adding a new command family.
- Implement every G4 condition from DEVELOPMENT_PLAN.md §8 as its own reported line: `pass`, `fail`, or `unmeasured`.
- Prove by fixture that a condition this command cannot measure never silently reports `pass`.
- Do not implement any action that changes corpus diversity or principal count; this command only reads existing state.

PER-PR GATES
Run the tests relevant to that PR plus the full `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest -q` gate on every PR.

REVIEW LOOP
Review each PR for overstated readiness claims, unmeasurable conditions silently passing, and import-boundary leakage between the `evaluation` and `engine` layers. Address only organic reviewer feedback; never trigger bots.

MERGE DISCIPLINE
Read the stacked-prs skill first. Base origin/main with the current default branch. Topology: origin/main -> m18/gate-status-01 -> m18/gate-status-02 -> m18/gate-status-03. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, then clean branches.

RELEASE PREP
RELEASE PREP: not-required. No version, changelog, tag, or publication work.

FINAL VERDICT
GO only if all three PRs are externally merged, the command reports the true current state of every G4 condition against a real local ledger, no condition is ever reported `pass` without durable supporting data, and the full gate passes on the clean default branch. Otherwise NO-GO.

RETURN
Return merged PRs, the command's real output against this repository's own current ledger state, commands/results, release state, and GO/NO-GO.
```

## M19 — Close the pilot-identified Stage 1/2/3 operator-experience gaps

```text
/goal
You are executing M19: close the three concrete CLI gaps the real G1 pilot (docs/pitch.md) identified and that remain open as of v0.3.0.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M19, §§2, 4
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/FUTURE_ENHANCEMENTS.md §5
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/pitch.md ("Operator burden" pilot finding)
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/operations.md (current CLI command surface)

TARGET
Add a guided Stage 1 request-builder command, a workspace inspect/release command pair, a fault-injection test resolving the queued-node stuck-recovery question, and Stage 2/3 broker coverage in `doctor`/`adapter doctor`. Do not change Stage 1/2/3's workflow behavior, policy, or evidence contracts.

RELEASE TARGET
`v0.4.0` per DEVELOPMENT_PLAN.md §2's 2026-08-04 DECISION and §4, assigned after this milestone had already merged. This milestone still performs no version bump, changelog entry, tag, or publication — M19b owns all of them for this train. Historical note: this block originally read `> GAP: unresolved`, and M19 was implemented and merged under that framing.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`; an invalid assumption, missing evidence, or changed safety requirement is `REPLAN REQUIRED`.
- Do not proceed even after `GO` until a human approves the reassessment entry.
- M8, M12, and M13 must be externally merged (they are, as of `v0.3.0`).
- The workspace-release command must use the identical fenced-proof discipline `CoordinatorRuntime.release_workspace` already enforces internally — never a weaker CLI-only check.
- Do not invent a fix for the queued-node stuck-recovery question before the fault-injection test in scope below determines whether one is actually needed.

SCOPE
A Stage 1 request-builder CLI command (guided or flag-driven, producing a `--request`-valid JSON document); a workspace-inspection CLI command listing current reservations; a workspace-release CLI command for a reservation with no live lease; a fault-injection test for a `queued` node stuck past its registering tick; extended `doctor`/`adapter doctor` output covering the Stage 2 release broker and the Stage 3 `LocalServiceDeploymentAdapter`'s configuration sanity.

STACK DEPTH
4 PRs.

PLANNED STACK
1. feat(cli): add a guided Stage 1 request-builder command
2. feat(cli): add workspace inspect and fenced-proof release commands
3. test(engine): add the queued-node stuck-recovery fault-injection test and its resulting documented behavior
4. feat(cli): extend doctor and adapter doctor with Stage 2/3 broker coverage

REQUIRED WORK
- Re-read `CoordinatorRuntime.release_workspace`'s exact fenced-proof checks before writing the CLI wrapper; reuse them, do not reimplement a weaker version.
- Prove the request-builder's output is accepted unmodified by `stage1 start --request`.
- Record the queued-node fault-injection result exactly as observed; if it reveals no gap beyond the existing `stage1 cancel` path, document that as the accepted limit rather than inventing unnecessary scope.
- Extend `doctor`/`adapter doctor` without requiring a live GitHub/PyPI/local-service network call.

PER-PR GATES
Run the tests relevant to that PR plus the full `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest -q` gate on every PR.

REVIEW LOOP
Review each PR for destructive-path safety (the workspace-release command specifically), CLI ergonomics matching the pilot's actual reported friction, and documentation drift in `docs/operations.md`. Address only organic reviewer feedback; never trigger bots.

HUMAN REVIEW GATE: Do not merge or run the workspace-release command's destructive path unattended until a human reviews its dry-run output, the fenced-proof check it reuses, and rollback notes.

MERGE DISCIPLINE
Read the stacked-prs skill first. Base origin/main with the current default branch. Topology: origin/main -> m19/operator-experience-01 -> m19/operator-experience-02 -> m19/operator-experience-03 -> m19/operator-experience-04. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, then clean branches.

RELEASE PREP
RELEASE PREP: not-required. No version, changelog, tag, or publication work.

FINAL VERDICT
GO only if all four PRs are externally merged, the request-builder's output round-trips through `stage1 start` unmodified, the workspace-release command never releases a live-leased reservation in the fault-injection test, the queued-node question has a recorded, documented answer, doctor coverage is extended, and the full gate passes. Otherwise NO-GO.

RETURN
Return merged PRs, the request-builder's example output, the workspace-release fenced-proof test result, the queued-node fault-injection finding, doctor output samples, commands/results, release state, and GO/NO-GO.
```

## M19b — Prepare, publish, and verify v0.4.0

```text
/goal
You are executing M19b: perform the single v0.4.0 release-preparation pass for the already-merged M18/M19 surface, publish to PyPI and GitHub Releases, and verify that the new CLI commands are reachable from a clean consumer install.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M19b, §2's 2026-08-04 release-train DECISION, and §§4 and 6 release rules
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§9, 11–13, 17–18
- Current repository release conventions, PyPI metadata, GitHub release configuration, and current official uv/GitHub CLI documentation

TARGET
Publish exactly the already-implemented M18 and M19 additions as v0.4.0, layered on the already-published v0.1.0, v0.2.0, and v0.3.0. Add no features, provider expansions, schema changes, or contract redesign. This release does not include M14b/M15 (gate-deferred) and does not package anything from M20–M23 — those shipped as repository tooling and published evidence, which the wheel never contains.

RELEASE TARGET
v0.4.0. This milestone owns the only product version update, CHANGELOG.md entry, tag, PyPI publication, and GitHub Release for this train.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers (for example `03_SYSTEM_DESIGN.md §9.5`) from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- The mandatory pre-milestone reassessment gate is ALREADY COMPLETE for this milestone: `GO` with a scope amendment, human-approved 2026-08-04, recorded in `.docs/MILESTONE_REASSESSMENTS.md` under "M19b". Do NOT re-run it and do NOT append a second entry. Read that entry first — it is the reason this stack is 4 PRs rather than 3.
- What the gate found, so you do not have to rediscover it: `scripts/release_gate.py` accepts `--version 0.4.0`; `scripts/full_system_gate.py --stages 1,2,3` is unchanged; `scripts/check_docs_currency.py` is genuinely wired into the gate and passes today. It also invalidated this milestone's original stated risk. The anticipated false-positive on historical `CHANGELOG.md`/`docs/RELEASE_EVIDENCE.md` entries CANNOT occur — `EXCLUDED_DOCS` excludes both files wholesale. The real defect is a FALSE-NEGATIVE: simulating the `0.3.0` -> `0.4.0` transition against the real tracked corpus produces failures only in `docs/operations.md` (`:10` and `:57`) and NONE in `README.md`, even though `README.md`'s Status section declares `v0.3.0` current. The four `STALE_SELF_VERSION_PATTERNS` are literal sentence forms, and two of the four match nothing anywhere in the corpus. PR 1 fixes this before any version bump.
- If any assumption in that entry no longer holds against the current default branch (for example a newer commit changed `check_docs_currency.py` or the docs), stop with `NO-GO — MILESTONE REASSESSMENT STALE` and request a fresh gate rather than proceeding on a stale approval.
- M18 and M19 must be externally merged to origin/main (PRs #140–#146, merged 2026-07-22) on top of the already-published v0.3.0. Do not wait for M14b or M15 — they are gate-deferred. Do not wait for anything in M20–M23 — all four are already merged and none is packaged.
- Obtain interactive human approval before irreversible PyPI/GitHub publication.
- PyPI versions and published tags are immutable; never delete/reuse/rewrite v0.4.0 after publication.
- Use uv only for Python environment/build/publish operations.
- No feature work in release prep.

SCOPE
A pre-release corrective change to scripts/check_docs_currency.py generalizing its self-version detection so a real 0.3.0 -> 0.4.0 transition fails closed on README.md, removing the two detection patterns with zero corpus hits, plus a mutation-verified regression test asserting detection against the real tracked corpus; canonical version update 0.3.0 -> 0.4.0; a `## [0.4.0]` CHANGELOG.md section authored retroactively from the merged M18/M19 diff (there is deliberately no standing `Unreleased` accumulator); README.md and docs/ currency sync to 0.4.0 inside this stack; release notes; compatibility/known-limit statement (must state that this release adds no workflow stage, that self-improvement is not implemented and Stage 4 is gate-deferred behind G4 with no committed date, and that M20–M23's tooling and evidence are intentionally not part of the distribution); docs/DEPENDENCIES.md and docs/RELEASE_EVIDENCE.md updates for this version; build; wheel/sdist checks; hashes; clean install on macOS/Ubuntu; CLI smoke of every command M18/M19 added; annotated tag; uv publish; GitHub Release; destination verification; post-publication evidence. Out of scope: any product-code change under src/enginery; widening check_docs_currency.py beyond self-version detection; anything from M20–M23, none of which is packaged; Stage 4.

STACK DEPTH
4 PRs (amended from 3 by the approved reassessment; PR 1 is the pre-release corrective PR the gate required).

PLANNED STACK
1. fix(release): make the docs-currency check catch a real version transition
2. build(release): prepare v0.4.0 version, changelog, and dependency manifest
3. docs: sync README and operator docs to v0.4.0
4. docs(release): finalize v0.4.0 compatibility statement and release notes

REQUIRED WORK
- PR 1 first, before any version bump. Generalize `check_docs_currency.py`'s self-version detection so a real `0.3.0` -> `0.4.0` transition fails closed on `README.md`; remove the two patterns with zero corpus hits rather than leaving dead guards; keep the stale-status-phrase list out of scope. Its regression test must assert detection against the REAL tracked corpus at a bumped canonical version, not a synthetic fixture alone, and it must be mutation-verified: confirm it FAILS against the pre-fix pattern set before you accept it. A test that passes both before and after the fix proves nothing.
- Re-read the actual repository release conventions and package metadata before editing.
- Derive the `[0.4.0]` changelog section from the real merged diff of PRs #140–#146, not from the plan's phrasing of what M18/M19 were supposed to deliver. Verify every claimed command exists by running `uv run enginery <command> --help`.
- The doc sync is inside this stack, not a follow-on: `release_gate.py` fails while any tracked doc still describes `v0.3.0` as current — and after PR 1 it will actually detect `README.md`, which it previously missed. Sync README.md's Status and CLI blocks and every affected `docs/*.md` file in PR 3, then re-run the gate. Before PR 3, confirm the check reports `README.md` among its failures at canonical `0.4.0`; after PR 3, confirm zero failures.
- Build once from the final intended origin/main commit; verify artifact metadata and hashes. Because the packaged surface merged on 2026-07-22, well before this release, explicitly confirm the build commit equals the verified origin/main tip — never build from a stale local checkout.
- Clean-install the wheel/sdist on macOS and Ubuntu and execute version, doctor, and the cumulative Stage 1+2+3 gate (`full_system_gate.py --stages 1,2,3`) smoke.
- From a scratch directory against the PUBLISHED 0.4.0 package (never the local checkout), invoke `enginery gate status --gate G4`, `enginery stage1 build-request --help`, `enginery workspace inspect --help`, `enginery workspace release --help`, and `enginery adapter doctor --json`, and confirm the last reports Stage 2/3 broker entries. This is the specific proof that the 2026-07-22 cadence gap is closed; a checkout-only smoke does not satisfy it.
- Create the v0.4.0 tag only after all four PRs merge and the exact main commit is green.
- After human approval, publish with uv and create the GitHub Release; then query both destinations and clean-install from PyPI.

PER-PR GATES
Run uv run python scripts/release_gate.py --version 0.4.0, uv run python scripts/check_docs_currency.py, uv build, uvx twine check dist/*, artifact hash verification, clean-install smoke, full lint/type/test/E2E gates, and CI. Never accept a stale artifact built from another commit.

REVIEW LOOP
Perform pre-landing, security, package metadata, changelog, and numeric-claim review. Confirm the release notes match checked evidence, claim no new workflow stage, and make no hosted/container/Windows/production-security/self-improvement claims. Address organic reviewer feedback only; never request bot review.

MERGE DISCIPLINE
Read stacked-prs and the applicable release-publish skill before git topology or publication. Base origin/main with M18–M23 merged, on top of the published v0.3.0 (M13b). Topology: origin/main -> m19b/release-01 -> m19b/release-02 -> m19b/release-03 -> m19b/release-04, where release-01 is the docs-currency corrective PR. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, clean branches, then verify origin/main exact commit before tagging. Never publish from a feature branch.

RELEASE PREP
RELEASE PREP: pending -> ready only when all four PRs are externally merged, release_gate passes on origin/main, artifacts are built from that commit, and human approval is recorded. After publication and destination verification, state becomes completed. Any failure is NO-GO; do not partially relabel it ready.

FINAL VERDICT
GO only if v0.4.0 is tagged from the verified main commit, PyPI and GitHub Release contain the intended artifacts/hashes, a clean PyPI consumer passes both the Stage 1+2+3 workflow smoke AND the five new-command smoke above, the docs-currency check both detected README.md before the sync and reports zero failures on the release commit, all branches are clean, release notes correctly scope this release and disclose the Stage 4 gate deferral, and release evidence is recorded. Before publication approval or with any mismatch, return NO-GO and preserve the unpublished or partially published state for remediation.

RETURN
Return the four merged PRs, the docs-currency before/after detection output (README.md must appear before the sync and be absent after), exact commit/tag, changelog/release-note paths, build artifact names/hashes, macOS/Ubuntu clean-install results, the published-package new-command smoke output, PyPI and GitHub URLs, publication approval record, RELEASE PREP completed or NO-GO state, and final verdict.
```

## M20 — Add a docs-currency and release-tooling-completeness check to the release gate

```text
/goal
You are executing M20: add an automated docs-currency check and a verification-tooling-completeness convention to the release gate, closing the two failure patterns that recurred across this repository's own delivery record.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M20, §§2, 4, 6
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/FUTURE_ENHANCEMENTS.md §7
- /Users/druk/WorkSpace/AetherForge/Enginery/scripts/release_gate.py (current structure)

TARGET
Build `scripts/check_docs_currency.py`, wire it into `scripts/release_gate.py`, and update this plan's own "Mandatory pre-milestone reassessment gate" paragraph (DEVELOPMENT_PLAN.md §2) to require a verification-tooling-completeness grep for every future release-preparation milestone. Do not touch product code or already-shipped doc content.

RELEASE TARGET
`none`. `scripts/` is never packaged into the wheel; this ships as repository tooling with no version bump, no changelog entry.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design, branch creation, or code changes, execute the mandatory pre-milestone reassessment gate. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`.
- Do not proceed even after `GO` until a human approves the reassessment entry.
- No product-code change. No change to already-shipped doc content — this milestone builds the check, not another doc-sync pass.
- The stale-phrase list must exclude `CHANGELOG.md`'s and `docs/RELEASE_EVIDENCE.md`'s own historical sections explicitly; false-positiving against legitimate historical text is a real risk named in the plan.

SCOPE
`scripts/check_docs_currency.py`: fails closed if a tracked doc contains the previous product version number outside a changelog/evidence file, or a configurable stale-status phrase outside the two excluded files' historical sections; its wiring into `scripts/release_gate.py`; the reassessment-gate paragraph update in `.docs/DEVELOPMENT_PLAN.md`.

STACK DEPTH
2 PRs.

PLANNED STACK
1. feat(release): add check_docs_currency.py with stale-version and stale-phrase fixtures
2. build(release): wire the docs-currency check into release_gate.py

REQUIRED WORK
- Re-read the current `docs/` tree and `scripts/release_gate.py`'s structure before writing the check.
- Build both a rejection fixture (a doc containing a stale reference) and an acceptance fixture (the current, already-corrected doc set) — prove both.
- Update `.docs/DEVELOPMENT_PLAN.md`'s reassessment-gate paragraph to require the verification-tooling-completeness grep; this is a `.docs/` edit, not a tracked-repository change.

PER-PR GATES
Run `uv run pytest tests/system/test_check_docs_currency.py -q`, `uv run python scripts/check_docs_currency.py`, plus the full `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest -q` gate on every PR.

REVIEW LOOP
Review for false-positive risk against legitimate historical text and for whether the check actually would have caught the two real incidents this milestone is named after (grep `.docs/MILESTONE_REASSESSMENTS.md`'s M12b and M13b entries for the exact failure pattern before declaring the check sufficient). Address only organic reviewer feedback; never trigger bots.

MERGE DISCIPLINE
Read the stacked-prs skill first. Base origin/main with the current default branch. Topology: origin/main -> m20/docs-currency-01 -> m20/docs-currency-02. Validate first parent/source. Merge root-to-leaf with fresh CI, retarget safely, then clean branches.

RELEASE PREP
RELEASE PREP: not-required. No version, changelog, tag, or publication work.

FINAL VERDICT
GO only if both PRs are externally merged, the check fails closed against a real stale-doc fixture, passes against the current doc set, is wired into `release_gate.py`, and the full gate passes. Otherwise NO-GO.

RETURN
Return merged PRs, the check's pass/fail output against both fixtures and the real current doc tree, commands/results, release state, and GO/NO-GO.
```

## M21 — Publish the recorded fault-injection recovery demonstration

```text
/goal
You are executing M21: produce and publish the standalone fault-injection recovery demonstration that strategy.md §5 names as the launch wedge artifact and gate G1's own pass condition requires.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M21, §§2, 4, 8 (gate G1)
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/FUTURE_ENHANCEMENTS.md §8
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/strategy.md §5
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/pitch.md (existing pilot record, for tone/rigor precedent)

TARGET
Record and publish a reproducible demonstration of a real coordinator interruption and reconciliation-driven recovery, with zero duplicate side effects, and a published evidence bundle. Do not add any new product feature and do not claim anything beyond what this repository's own already-produced evidence supports.

RELEASE TARGET
`none`. A published demo artifact, not a package release.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before detailed design or any live-provider action, execute the mandatory pre-milestone reassessment gate. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`.
- Do not proceed even after `GO` until a human approves the reassessment entry.
- M8 must be externally merged (it is). Opt-in live-provider credentials are required, gated exactly like `tests/provider_smoke` — never invoke a live mutation without the same explicit allowlist discipline M7/M8 already established.
- Do not publish a staged or simulated-looking recovery as if it were the real thing; the recording must show an actual coordinator interruption against a real or realistic fixture.

SCOPE
A demo script or runbook reproducing a real coordinator-interruption-and-recovery sequence; a published write-up or recording; a link from README.md/docs/pitch.md to the published artifact once it exists.

STACK DEPTH
1 PR.

PLANNED STACK
1. docs(pitch): record and publish the fault-injection recovery demonstration

REQUIRED WORK
- Re-verify the allowlisted smoke-fixture repository and credentials are still valid before recording anything live.
- Record a real interruption-and-recovery sequence, not a scripted narration of a past run.
- Publish the resulting artifact somewhere reachable outside this repository (per strategy.md §5's named launch channels), then link it from README.md/docs/pitch.md.

PER-PR GATES
No product code changes; run the full `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest -q` gate anyway to confirm nothing was inadvertently touched.

REVIEW LOOP
A human reviews the published artifact against the "no simulation-only" bar M13's own FINAL VERDICT convention already applies to Stage 3, before it is linked from any tracked doc.

HUMAN REVIEW GATE: Do not publish, or link from any tracked document, until a human confirms the recording/runbook reflects a real, not staged, recovery sequence, and confirms no credential or secret value appears in the published artifact.

MERGE DISCIPLINE
Read the stacked-prs skill first. Base origin/main with the current default branch. Topology: origin/main -> m21/recovery-demo-01. Validate first parent/source. Merge only after fresh CI and the human review gate above.

RELEASE PREP
RELEASE PREP: not-required. No version, changelog, tag, or PyPI/GitHub Release publication of the product package.

FINAL VERDICT
GO only if the demonstration is genuinely reproduced (not staged), published somewhere reachable, linked from README.md/docs/pitch.md, and the human review gate is satisfied. Otherwise NO-GO.

RETURN
Return the published artifact's URL, the human review confirmation, the PR link, commands/results, release state, and GO/NO-GO.
```

## M22 — Run and publish the Stage 2 + Stage 3 pilot comparison protocol

```text
/goal
You are executing M22: extend the existing Stage-1-only pilot record with a comparable manual-baseline-versus-Enginery comparison for a real Stage 2 cycle and a real Stage 3 cycle, using the same documented comparison protocol docs/pitch.md already established.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M22, §§2, 4
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/FUTURE_ENHANCEMENTS.md §8
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/pitch.md ("Pilot results (2026-07-20)" and its comparison protocol/decision rule)

TARGET
Run the existing comparison protocol against one real Stage 2 work item (a real, disposable fixture release, mirroring M12's own discipline) and one real Stage 3 work item (a real controlled-local-service incident, mirroring M13's own discipline), and publish a comparable write-up. Do not add any new product feature.

RELEASE TARGET
`none`. A published pilot write-up, not a package release.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before any live-provider or fixture-publish action, execute the mandatory pre-milestone reassessment gate. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`.
- Do not proceed even after `GO` until a human approves the reassessment entry.
- M12 and M13 must be externally merged (they are). Opt-in live-provider/local-service credentials required.
- The Stage 2 fixture publish must reuse M12's disposable-fixture-distribution discipline exactly — never the real `enginery` package name or version, and PyPI/GitHub publication is irreversible.
- The Stage 3 run only ever targets the controlled local service, never a real destination.

SCOPE
A Stage 2 comparison run (manual baseline vs. Enginery-orchestrated) and its evidence bundle; a Stage 3 comparison run and its evidence bundle; a published write-up appended to or alongside docs/pitch.md, matching its existing "Pilot results" section's level of detail.

STACK DEPTH
1 PR.

PLANNED STACK
1. docs(pitch): record the Stage 2 and Stage 3 pilot comparison results

REQUIRED WORK
- Re-confirm the disposable-fixture-distribution discipline from M12 and the controlled-local-service discipline from M13 are still current before running anything live.
- Apply the same go/no-go decision rule docs/pitch.md already defines to both new comparison runs.
- State elapsed time, intervention count, and burden for both the manual baseline and the Enginery path in each case; make no productivity claim beyond what the recorded numbers support.

PER-PR GATES
No product code changes; run the full `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest -q` gate anyway to confirm nothing was inadvertently touched.

REVIEW LOOP
A human reviews the published write-up against docs/pitch.md's existing comparison-protocol rules before publication, specifically checking that no claim exceeds the recorded numbers.

HUMAN REVIEW GATE: Do not execute the real Stage 2 fixture publish or link the published write-up from any tracked document until a human reviews the fixture-distribution name/version for collision with the real product and confirms the Stage 3 run only touched the controlled local service.

MERGE DISCIPLINE
Read the stacked-prs skill first. Base origin/main with the current default branch. Topology: origin/main -> m22/pilot-comparison-01. Validate first parent/source. Merge only after fresh CI and the human review gate above.

RELEASE PREP
RELEASE PREP: not-required. No version, changelog, or tag work for the real `enginery` package; the Stage 2 comparison's own disposable fixture publish is scoped exactly like M12's, never the real product.

FINAL VERDICT
GO only if both comparison runs are genuinely executed against real infrastructure (a real disposable fixture destination for Stage 2, the real controlled local service for Stage 3), the write-up matches the recorded evidence exactly, and the human review gate is satisfied. Otherwise NO-GO.

RETURN
Return both evidence bundles, the published write-up's location, the human review confirmation, commands/results, release state, and GO/NO-GO.
```

## M23 — Hands-on competitive capability matrix

```text
/goal
You are executing M23: produce a hands-on, dated capability matrix verifying the closest control-plane entrants against the same scenarios this repository's own adversarial gates already define, before any comparative or uniqueness claim ships in marketing copy.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M23, §2
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/FUTURE_ENHANCEMENTS.md §8
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/overview.md §7 ("Differentiation evidence required")
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/strategy.md §5 ("Claim discipline")

TARGET
Produce a written, dated capability matrix recording direct, hands-on observations of the closest control-plane entrants (at minimum OpenHands' Agent Control Plane, Databricks' Omnigent, and Guild.ai — re-verify which are still active at research time) against the ambiguous-side-effect, stale-evidence, and approval-supersession scenarios this repository's own adversarial gates already define. No product-code change; no marketing copy itself.

RELEASE TARGET
`none`. A research document, not a package release.

GLOBAL CONSTRAINTS
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation. `.docs/` is globally gitignored planning material and never enters the implementation repository; state design rationale in prose instead.
- Before starting, execute the mandatory pre-milestone reassessment gate to re-check which entrants are still active and relevant. Append an evidence-backed entry to `.docs/MILESTONE_REASSESSMENTS.md`.
- Every claim in the matrix is either a first-hand observation against a real running instance of the entrant, or explicitly marked "not independently verified" with its secondary source — never inferred from marketing copy alone.
- Do not let this milestone's own output be cited by any public copy until the human review below is complete.

SCOPE
A dated capability-matrix document (for example docs/competitive-capability-matrix.md) with one row per entrant per scenario and a primary-source citation or an explicit "not independently verified" mark.

STACK DEPTH
1 PR.

PLANNED STACK
1. docs(research): publish the hands-on competitive capability matrix

REQUIRED WORK
- Re-verify which control-plane entrants are still active and comparable at research time before starting; do not assume the three named in FUTURE_ENHANCEMENTS.md §8 are still the complete or correct set.
- Attempt direct reproduction against each entrant for each scenario before marking any row "not independently verified" — that mark is a fallback, not a default.
- Cite a primary source for every claim; per docs/overview.md §7, secondary-source absence is not evidence of absence.

PER-PR GATES
No product code changes; run the full `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, `uv run pytest -q` gate anyway to confirm nothing was inadvertently touched.

REVIEW LOOP
A human reviews every row for citation quality and for whether "not independently verified" was used as an honest fallback rather than a shortcut around a claim that should have been reproduced directly.

HUMAN REVIEW GATE: Do not let any public-facing copy cite this matrix, and do not merge a version that overstates a competitor gap, until a human confirms every row's classification.

MERGE DISCIPLINE
Read the stacked-prs skill first. Base origin/main with the current default branch. Topology: origin/main -> m23/capability-matrix-01. Validate first parent/source. Merge only after fresh CI and the human review gate above.

RELEASE PREP
RELEASE PREP: not-required. No version, changelog, tag, or publication work.

FINAL VERDICT
GO only if every row in the matrix cites either a direct reproduction or an explicit unverified mark with its secondary source, and the human review gate is satisfied. Otherwise NO-GO.

RETURN
Return the merged PR, the published document's path, the human review confirmation, and GO/NO-GO.
```

## M24 — Make Gate G4 evidence measurable and fail closed *(`v0.5.0` train)*

```text
/goal
You are executing M24: correct the unreachable G4 measurement surface so it can evaluate real, source-bound operational evidence without manufacturing that evidence or starting Stage 4.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M24 and §§2, 4, 6, 8
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§5, 8–9, 17
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/workflows.md
- Current GitHub adapter, Stage 1 workflow, ledger serialization/migration, authority, outcome, gate, CLI, and test contracts
- Current official GitHub CLI/REST documentation for issue labels and pull-request reviews

OBJECTIVE
Make G4 measurable and fail closed: bind closed declared GitHub classifications into a source snapshot; support bounded classified Stage 1 work; authenticate one recurring-deficiency finding through exact GitHub evidence-PR reviews from two configured immutable GitHub numeric identities; and calculate every quantitative condition from one verified classified cohort. This milestone must not report G4 passed without genuine post-merge operational evidence.

RELEASE TRAIN: target=v0.5.0; included milestones=M24; preparation trigger=M24 externally merged and v0.4.0 published; required artifacts=version update, CHANGELOG.md, source archive, wheel, sdist, dependency manifest, release notes, release evidence; release verification=release_gate, full quality gate, labeled Stage 1 smoke, gate-evidence adversarial checks, clean installs, published-consumer smoke; publication=M24b after a human publication gate.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/MILESTONE_REASSESSMENTS.md`.
2. Inspect the published v0.4.0 baseline, merged M18 implementation, current GitHub API/CLI payload behavior, and the current Stage 1, authority, ledger, migration, and gate contracts.
3. Revalidate the closed label vocabulary, source-revision binding, supported Stage 1 kinds, GitHub numeric identity mapping, exact-review semantics, deficiency-evidence shape, cohort eligibility, acceptance, release train, and M24b/M14b/M15 impact.
4. Append one ledger entry: timestamp, M24, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M24, M24b, M14b, and M15, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This records a completed diagnosis but blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M24 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

CONSTRAINTS:
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation.
- Require exactly one `enginery/work-kind/{issue,plan}` label and exactly one `enginery/risk/{low,medium}` label. Reject absent, unknown, conflicting, duplicate, and case-variant labels; never default or infer classification.
- Store canonical labels and classification provenance in the serialized, bound source snapshot. Legacy/unlabeled or manually constructed runs are ineligible for M24 G4 diversity counts.
- Cleanly replace issue-only qualification terminology and callers. Support only `issue` and `plan`; keep `incident`, `milestone`, `factory_change`, high-risk Stage 1 work, new source providers, and all Stage 4 capability out of scope.
- A deficiency finding must cite at least two distinct eligible verified-complete runs and durable ledger references. A local flag, TOML value, or self-authored record never proves dual authority.
- Require a merged evidence PR, exact merged-document digest, two current head-bound GitHub approvals from configured distinct immutable GitHub numeric identities, producer/author separation, and fail-closed GitHub API errors. Configuration migration must block unsupported legacy schema rather than silently weakening authority.
- Preserve immutable run, outcome, intervention, and evidence history. Never backfill labels, rewrite history, or claim an external adopter signal the product cannot verify.

PLANNED STACK:
0. Conditional prerequisite `docs(plan): reconcile M24 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 `feat(github): bind declared classifications to source snapshots` — scope: closed label parsing, classification provenance, serialization/migration, GitHub adapter contract tests; commits: `feat(github): bind labeled work classification to issue snapshots`, `test(github): reject ambiguous source classifications`; verification: focused GitHub adapter/domain/serialization tests.
2. PR-2 `refactor(stage1): qualify supported labeled work items` — scope: clean Stage 1 terminology cutover, supported `issue`/`plan` routing, medium-risk approval path, classified-cohort eligibility; commits: `refactor(stage1): replace issue-only qualification contract`, `test(stage1): cover labeled work and risk routing`; verification: focused workflow/gate tests.
3. PR-3 `feat(gate): persist immutable G4 deficiency findings` — scope: version-2 GitHub-mapped authority roster, finding aggregate/serialization/migration, cited-run/evidence validation, pure gate evaluation; commits: `feat(gate): add durable deficiency evidence`, `test(gate): reject unverifiable finding inputs`; verification: focused ledger/gate/migration tests.
4. PR-4 `feat(github): verify dual-authority evidence reviews` — scope: GitHub evidence-PR/review reader, exact-head verification, gate-recording CLI, status evidence output; commits: `feat(github): read evidence pull-request approvals`, `feat(cli): record and report G4 deficiency evidence`; verification: focused adapter/CLI tests with paginated review fixtures.
5. PR-5 `docs(gate): document measurable G4 operation` — scope: operator documentation, config migration guide, label/evidence-PR procedure, adversarial and mutation-verified regression coverage; commits: `test(gate): adversarially verify G4 evidence guards`, `docs(gate): document classified evidence collection`; verification: full M24 acceptance gate and human review of the documentation procedure.

VERIFICATION (must pass):
- Focused GitHub, Stage 1, gate, CLI, migration, outcome, and serialization tests exit 0.
- Every guard has a mutation proof: removing label uniqueness, source binding, eligible-run validation, merged-PR validation, exact-head review binding, distinct registered approvers, author/producer separation, or evidence-digest matching makes its named regression test fail.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, and `uv run pytest -q` exit 0.
- An opt-in approved GitHub fixture proves actual label fetch and review-payload parsing; it does not create a fabricated G4 pass or mutate an unapproved repository.

REVIEW:
Per PR:
- Scope matches its stated responsibility; domain remains provider-neutral and provider vocabulary remains at the adapter boundary.
- Missing/ambiguous/stale data fails loudly; no default classification, unauthenticated approver, stale approval, or local substitute survives.
- Serialization/migration compatibility and source-revision invalidation are explicit and tested.
- PR-specific verification output is captured; history is atomic, conventional, attribution-free, and free of unrelated formatting churn.
Whole stack:
- Bases form one valid stack; the full cohort/evidence contract is coherent across adapter, domain, ledger, gate, CLI, migration, and docs.
- Full quality gates, adversarial checks, mutation proofs, and CI are green.
- Review label vocabulary, GitHub review semantics, auth/producer separation, immutable-history handling, and documentation claims against the exact merged behavior.

FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: v0.5.0 — RELEASE PREP: pending` or `NO-GO — RELEASE: v0.5.0 — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, five correctly based/reviewed/green PRs, all M24 verification, and zero claim that operational G4 evidence has already passed.

NEXT STEPS:
1. Current milestone: merge the reviewed M24 stack.
2. Release: begin M24b only after M24 merges and `v0.4.0` remains verified as published.
3. Next milestone: collect real post-M24 operational evidence and re-run G4; do not start M14b/M15 until it passes.
4. For `NO-GO`: repair the named guard or release-plan mismatch, then repeat the design gate and full acceptance suite.

DONE: design verdict with evidence; when authorized, a reviewed five-PR stack with a release-aware merge verdict and the required next-steps list.
```

## M24b — Prepare, publish, and verify `v0.5.0`

```text
/goal
You are executing M24b: perform the one v0.5.0 release-preparation pass for the merged M24 measurable-G4-evidence surface, publish it, and prove its CLI/configuration surface from clean consumers.

SOURCE OF TRUTH
- /Users/druk/WorkSpace/AetherForge/Enginery/.docs/DEVELOPMENT_PLAN.md M24b and §§2, 4, 6
- /Users/druk/WorkSpace/AetherForge/Enginery/docs/design.md §§5, 8–9, 17–18
- Current release conventions, package metadata, GitHub release configuration, and official uv/GitHub CLI documentation

OBJECTIVE
Publish exactly M24 as v0.5.0. Do not add workflow stages, providers, operational evidence, G4 passage, cohort/replay, candidate evaluation, canary, promotion, or Stage 4 behavior.

RELEASE TRAIN: target=v0.5.0; included milestones=M24; preparation trigger=M24 externally merged and v0.4.0 published; required artifacts=version update, CHANGELOG.md, source archive, wheel, sdist, dependency manifest, release notes, release evidence; release verification=release_gate, full quality gate, labeled Stage 1 smoke, build, clean installs, hashes, published-consumer smoke; publication=human-approved PyPI and GitHub Release.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/MILESTONE_REASSESSMENTS.md`.
2. Inspect the exact merged M24 diff, current origin/main, M24 verification/CI evidence, release scripts, package metadata, current docs, and v0.4.0 destination evidence.
3. Revalidate M24 command names, numeric GitHub identity configuration migration, release target, changelog content, platform/install commands, labeled fixture smoke, published-consumer smoke, M14b/M15 deferral wording, and every required release command.
4. Append one ledger entry: timestamp, M24b, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M24b and affected future milestones, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks release-prep code until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M24b design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before release-preparation code or docs, and must not be folded into a release PR.

CONSTRAINTS:
- Never reference `.docs/` files, filenames, or section numbers from implementation code, docstrings, comments, or tracked documentation.
- Build only from the verified origin/main commit after all M24 PRs merge; never publish a feature branch or stale checkout.
- The changelog and release notes derive from actual merged M24 diffs. State that v0.5.0 adds no workflow stage and that G4 still requires real multi-repository, dual-authority numeric-identity, intervention, outcome, and deficiency evidence before M14b/M15.
- Verify the release gate's own v0.5.0 command support and docs-currency coverage before changing version metadata. Its implementation must select public product documentation without hard-coding planning-artifact paths.
- PyPI versions and public tags are immutable. Obtain interactive human approval immediately before publication.

PLANNED STACK:
0. Conditional prerequisite `docs(plan): reconcile M24b design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 `build(release): prepare v0.5.0 metadata and dependency manifest` — scope: the justified release-tool currency-selection correction, its regression coverage, canonical version, changelog, dependency manifest, artifact metadata; commits: `fix(release): select public documentation for currency checks`, `build(release): prepare v0.5.0 metadata`, `docs(changelog): add v0.5.0 release entry`; verification: release gate, build, metadata, docs-currency, and hash checks.
2. PR-2 `docs: sync v0.5.0 operator documentation` — scope: README and operator docs, label/config migration, evidence-PR operation, compatibility/known-limit statement; commits: `docs: sync measurable G4 operation for v0.5.0`; verification: docs-currency check and command examples against the installed wheel.
3. PR-3 `test(release): prove v0.5.0 consumer installation` — scope: macOS/Ubuntu clean-install scripts, labeled Stage 1 fixture smoke, published-consumer command smoke; commits: `test(release): add v0.5.0 consumer smoke`; verification: platform-clean-install and fixture smoke evidence.
4. PR-4 `docs(release): finalize v0.5.0 notes and evidence` — scope: final release notes, hashes, dependency/evidence records; commits: `docs(release): record v0.5.0 publication evidence`; verification: destination verification after human-approved publication.

VERIFICATION (must pass):
- `uv run python scripts/release_gate.py --version 0.5.0`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src`, and `uv run pytest -q` exit 0.
- The labeled Stage 1 fixture smoke verifies source classification and the real medium-risk human-approval path without asserting a G4 pass.
- `uv build`, `uvx twine check dist/*`, artifact hash verification, and clean macOS/Ubuntu wheel/sdist installation succeed.
- From a scratch directory against the published package, invoke `enginery gate status --gate G4 --json`, `enginery gate record-g4-deficiency --help`, `enginery gate record-g4-deficiency-evidence --help`, and labeled Stage 1 command help. Confirm the command surface is reachable but G4 remains fail-closed without real evidence.
- After human approval, `uv publish`, `gh release create`, and `uv run python scripts/verify_published_release.py --version 0.5.0` succeed.

REVIEW:
Per PR:
- Scope matches release preparation; no M24 behavior change, no new workflow stage, and no product feature leaks into release-only work.
- Release claims, docs, migration guidance, consumer examples, hashes, and dependency data match checked artifacts.
- PR-specific verification is captured; history is atomic, conventional, attribution-free, and free of unrelated formatting churn.
Whole stack:
- Full release gate, labeled fixture smoke, platform installs, build checks, hashes, exact-main provenance, CI, and human publication approval are complete.
- Published package and GitHub assets are independently queried and match the recorded hashes.

FINAL VERDICTS:
- Report the design verdict before the publication verdict.
- Then report exactly one publication verdict: `GO — RELEASE: v0.5.0 — RELEASE PREP: completed` or `NO-GO — RELEASE: v0.5.0 — REASON: <blocking gate>`.
- `GO` requires v0.5.0 tagged from verified main, correct artifacts at both destinations, clean consumer smoke, release notes that preserve Stage 4 deferral, and recorded release evidence.

NEXT STEPS:
1. Current milestone: publish only after every release-preparation PR is merged, CI is fresh, and human publication approval is recorded.
2. Release: verify PyPI and GitHub destinations, then record v0.5.0 completion evidence.
3. Next milestone: collect the actual multi-repository, dual-human operational corpus; re-run G4 and keep M14b/M15 blocked until pass.
4. For `NO-GO`: preserve unpublished artifacts or partial-publication state, repair the exact mismatch, and repeat destination verification.

DONE: design verdict with evidence; when authorized, a reviewed four-PR release-preparation stack with a publication-aware verdict and the required next-steps list.
```
