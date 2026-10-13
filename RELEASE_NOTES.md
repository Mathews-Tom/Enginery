# Enginery `v0.6.0` Release Notes

**`v0.6.0` makes three things available to installed consumers: public documentation reconciled with what is actually shipped, typed check contracts with execution selection that records the model and effort a run asked for, and Stage 1 queue and worker recovery that no longer leaves work stuck. It adds no workflow stage.** Stage 1 (issue to merge-ready pull request), Stage 2 (plan to verified release), and Stage 3 (incident to hotfix and rollback) remain the shipped workflow surface.

## Breaking changes

- **Stage 1 requests built by `v0.5.0` must be rebuilt.** `enginery stage1 start` now rejects a new run whose request does not bind the current check contracts (manifest schema 2 or later, checks identical to the packaged acceptance baseline). A request written by `v0.5.0`'s `enginery stage1 build-request` uses manifest schema 1; rebuild it with the `v0.6.0` `enginery stage1 build-request` before starting the run. A run that was already started from such a request is read and resumed unchanged.
- **Validation commands that launch a shell are rejected.** `enginery stage1 start` refuses a request whose validation command launches a shell interpreter, directly or through a known launcher wrapper. This is an input guard, not a sandbox.
- **`ClaudeCodeAdapterConfig.model` is removed** (library API). The model and effort a run requests now come only from its per-task execution selection. When the selection names none, no flag is passed and Claude Code resolves its own default.
- **Request digests change for new runs.** The execution selection is part of a new Stage 1 request's state, so a request rebuilt with `v0.6.0` has a different digest from one built from the same inputs by `v0.5.0`. Requests persisted before this change keep their exact digest and decode as a bare host default.

## What ships in `v0.6.0`

- **Reconciled public documentation.** The README, operator guide, and design documents state what is published, label the proposed manual-first factory command family as proposed and not shipped (the CLI has no `enginery factory` command), and describe merge authority as the shipped policy.
- **Typed check contracts and a frozen acceptance baseline.** Workflow `run_command` and `verify_evidence` nodes bind a closed check declaration (stable id, bound subject, an argv command or a named observer, and a stop or repair disposition). Stage 1's manifest is schema 2 and binds `stage1.validate` and `stage1.verify`. A packaged acceptance baseline v1 is verified against its registered SHA-256 digest before it is parsed, and a new Stage 1 run must bind checks identical to it.
- **Execution selection.** `enginery stage1 build-request` accepts `--model`, `--effort`, and `--strict-effective`. Pins are exact: aliases, bare OMP models, effort levels outside the host's vocabulary, and a strict requirement with nothing pinned (or with `--effort`) exit with code 2 and write no file. The selection records which layer supplied each control and binds the run to a digest-frozen execution catalog. Both supported hosts are `experimental` in that catalog.
- **Native controls.** The OMP adapter submits `--model` and `--thinking`; the Claude Code adapter submits `--model` and `--effort`. A task is rejected before launch on a wrong pin form, an effort outside the host vocabulary, a selection for another harness or catalog, or an installed host whose version or protocol differs from the tested fingerprint. A run whose catalog or host fingerprint later drifts is held with a diagnostic.
- **Execution receipts.** `enginery stage1 watch` and `enginery stage1 evidence` print an `execution` block that keeps requested, selected, submitted, and effective controls apart. The effective model is reported only when the host reports it; effective effort is unknown on both hosts, and a requested value is never shown as an effective one.
- **Queued work is redispatched.** A Stage 1 implementation node that cannot run because another run holds the scheduling slot stays durably queued, and `enginery stage1 watch --advance` dispatches it exactly once when capacity frees, oldest-queued first, or records why it stayed queued. A new run's default implementation attempt id is `<run-id>-implement-0`, so a second run on a shared ledger no longer fails its lease.
- **Worker liveness and recovery.** Persisted worker identity is bound to the machine and boot, so a dead worker, a reused pid, a reboot, a live worker whose coordinator lapsed, a foreign host, and an unverifiable record are told apart. A proven-dead worker is failed with its identity evidence and frees its slot; an unprovable one holds and keeps it. Cancel works after a coordinator restart, is retryable, terminates the whole process group, and wins over a late result. Launched workers no longer inherit the coordinator's standard streams, so `stage1 watch --advance | reader` returns when the command does.

## Compatibility and known limitations

- `v0.6.0` publishes schema and API versions but makes **no `1.0` stability promise**. Runs bind adapter and configuration fingerprints and block silent resume under changed behavior.
- This release adds **no workflow stage**. Stage 1, Stage 2, and Stage 3 are the complete shipped workflow surface.
- **Not part of this release:** an isolated worker runner, live two-host qualification, and any `enginery factory` command. Both agent hosts stay `experimental`; no model or effort is endorsed, and a public model listing is not proof that an operator can use a model.
- **G4 remains fail-closed** and no G4 passage is recorded. It requires genuine multi-repository, dual-human numeric-identity, intervention, outcome, and recurring deficiency evidence. Stage 4 (governed factory self-improvement) is unimplemented and has no committed date. The frozen acceptance baseline v1 is unchanged by this release.
- There is no hosted or multi-tenant service, organization RBAC, browser dashboard, or interactive TUI.
- **Windows is not supported.** Process supervision, cancellation, and recovery are bound to POSIX process groups and signals.
- **Trusted-local execution is not containment.** The worktree backend provides workspace separation, not hostile-code containment.
- There are no dependency changes since `v0.5.0`.

## Installation

```bash
uv tool install enginery==0.6.0
enginery --version
enginery doctor
```

Requires Python 3.12+. Supports macOS and Linux.

## Links

- [`CHANGELOG.md`](CHANGELOG.md)
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)
- [`docs/operations.md`](docs/operations.md)
- [`docs/RELEASE_EVIDENCE.md`](docs/RELEASE_EVIDENCE.md)
