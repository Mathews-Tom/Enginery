# `v0.1.0` Release-Readiness Report — Stage 1 Only

**Scope of this report:** M16's own deliverable — Stage-1 cumulative
recovery evidence, a measured local performance baseline, and a summary of
what M16 leaves for M17. This report is not the M17 release-preparation
pass itself: `RELEASE PREP` for `v0.1.0` remains **pending**. The canonical
package version stays `0.0.0.dev0` (`pyproject.toml`); no changelog, tag,
or publication happens in this milestone.

## `v0.1.0` train status

`v0.1.0` requires M1-M8, M14a, and M16 externally merged, plus this
milestone's cumulative Stage 1 gate evidence. As of this report:

| Milestone | Status |
|---|---|
| M1-M8 | Externally merged (Stage 1 issue-to-merge-ready-PR implementation, corrective stack, and the retained live-gate evidence below) |
| M14a | Externally merged: PRs #113-#115 (`m14a/outcomes-01..03`), merged 2026-07-21 |
| M16 | This four-PR stack (`m16/stabilize-01..04`) |
| M17 | Not started. Begins only after M1-M8, M14a, and M16 are all externally merged and this report's cumulative gate passes. |

M9-M13/M12b (the `v0.2.0`/`v0.3.0` trains) and M14b/M15 are **explicitly
out of scope for `v0.1.0`**: M9-M12/M12b are separate, later release
trains; M14b and M15 are gate-deferred behind gate G4 (a data-threshold
entry gate — completed-run and intervention volume across at least two
workflow types and risk classes, an outcome-capture completeness floor, at
least one recurring evidence-backed workflow deficiency, corpus diversity
beyond a single repository, and a second registered human principal) and
may not start, including design work beyond the raw outcome schema M14a
already shipped, until that gate passes on a review cadence.

## Stage 1 gate evidence

Two independent evidence sources exist for the Stage 1 falsifiable gate
("a real issue yields a non-empty, current-head, evidence-complete
merge-ready PR; interruption does not duplicate effects; no-op work is
rejected"), and this report does not conflate them:

1. **Real-provider live evidence (M8, already retained, not repeated by
   M16):** issue #84 → PR #90 in `Mathews-Tom/Enginery`, head
   `75ccc19d024ec65636cb30bd1680bd9be176e75f`, `mergeStateStatus: CLEAN`,
   CI green on macOS and Ubuntu, intentionally left open and unmerged
   (Stage 1 stops before merge by design). Terminal `merge_ready` digest:
   `sha256:b7ee86840a284fe5af5dcd6dbe41c09b9a4a757b3fd91062c2456a8594bb3a0e`.
   This is real subprocess-level coordinator-replacement recovery against
   a real GitHub issue and a real OMP harness run.
2. **Cumulative local restart/replay evidence (this milestone):**
   `scripts/full_system_gate.py --stages 1 --restart-between-stages`,
   executed against this stack's own `m16/stabilize-04` head
   (`839f6ab`, based on `origin/main` at `c542e13`) on 2026-07-21T10:09:57Z:

   ```text
   PASS full-system-gate stages=1 restart_between_stages=True runs=2 \
     evidence_digest=sha256:d8fa4412a412513986e2bcdd045c84756be6889a03c7db8ba0d797b783400e8d
   ```

   This drives **two independent local work items** through the complete
   Stage 1 lifecycle (qualify → implement → validate → review → open PR →
   wait for CI → verify merge-ready → register outcome observation) on
   **one durable SQLite ledger**, closing every in-memory coordinator and
   service object and reopening it from durable state alone before every
   externally observable step. This reconstructs the coordinator exactly
   like this repository's established recovery-proof convention (a fresh
   `CoordinatorRuntime`/`Stage1RunService` over the same on-disk ledger,
   matching the recovery topology's coordinator-epoch/fencing model) — it
   is **not** a literal new operating-system process boundary. Both work
   items independently reach `merge_ready` with exactly one recorded
   pull-request `create_or_update` call each (no duplicate side effect
   across the restarts), and re-deriving the next action after termination
   is idempotently `wait`. Qualification, implementation dispatch, and
   validation are completed through direct durable node-state transitions
   in this script rather than the real executors, matching
   `tests/outcomes/test_dogfooding.py`'s established pattern — those three
   nodes' own crash/fault-injection coverage already lives in the merged
   M8 corrective stack, so this gate's job is specifically the
   review-through-outcome-registration chain, where a real run's
   externally observable claims live.

   The printed `evidence_digest` is a fresh SHA-256 over the two runs'
   final ledger-durable evidence (run status, aggregate version, request
   digest, pull-request-call count) computed on **every** invocation, not
   a fixed constant: each invocation uses a fresh temporary directory,
   and that directory's randomized path is embedded in the bound request
   digest by design (so two invocations can never collide on ledger
   state). The digest above is the value from the specific run recorded
   in this report; re-running the gate produces a new, equally valid
   digest with the same PASS outcome. `uv run pytest tests/e2e -q` wraps
   the same code as a fast (0.14s) CI-safe regression test — `3 passed`.

Only Stage 1 is gated here (`--stages` accepts `1` only in this
milestone's script). Stage 2-4 cumulative gates belong to their own
release trains and are not attempted by this report.

## Measured local performance baseline

`scripts/performance_baseline.py --assert-bounds
config/performance-bounds.toml`, executed on the same host and commit as
the gate run above (Apple M1 Pro, macOS-26.5.2-arm64):

```text
stage1_full_lifecycle=0.0532s bound<=2.0000s: PASS
ledger_append=6139.3events_per_second bound>=1000.0: PASS
ledger_verify=0.0028s bound<=0.5000s: PASS
PASS performance-baseline
```

These are **local, single-machine, Stage-1-only bounds for regression
detection on this class of hardware** — not a cross-platform performance
guarantee, not a production-load claim, and not a claim about Stage 2-4
performance, which remains unmeasured and unclaimed. Bounds in
`config/performance-bounds.toml` include headroom over the observed range
(three repeated measurements: `stage1_full_lifecycle` 0.05-0.15s,
`ledger_append` 5600-7600 events/second, `ledger_verify` 0.003-0.016s) for
CI-runner variance. The script fails closed — non-zero exit, no bound
silently treated as passing — whenever a required bound is missing from
the bounds file or a measurement cannot execute; this was verified
directly against a missing and an empty bounds file during PR4 review.

## Documentation delivered

- [`docs/operations.md`](operations.md) — operator install, configuration,
  doctor, the real CLI command surface, Stage 1 lifecycle, recovery
  semantics, backup/restore, and security limits (worktree isolation
  boundary, Stage 1's credential-reference boundary, the single-operator
  authority model and its Stage-4 dual-human limit, the documented
  merge-ready TOCTOU residual window, unsupported Windows behavior).
- [`docs/migration-sage-dev.md`](migration-sage-dev.md) — the manual,
  human-executed `sage-dev` migration guide with a mandatory
  backup-before-import step, verified end to end against a real
  constructed `sage-dev`-shaped fixture and this repository's real CLI.
- [`docs/adapters.md`](adapters.md) — the adapter authoring contract,
  grounded in the real port `Protocol` definitions, and the Armory
  capability-registry relationship.
- [`docs/examples.md`](examples.md) — two verified worked examples (a
  fully local no-live-provider request and a real GitHub/OMP-backed
  request).

## Verdict

**GO** for M16's own scope: the four planned PRs (`m16/stabilize-01..04`)
land documentation and gate evidence only, with no edits to existing
domain, engine, ledger, or policy modules; the manual migration guide is
verified human-executable and non-mutating in its inspection/request-
building steps, with the ledger backup mandatory before the one mutating
command; every documented CLI command was executed for real, not assumed;
every Mermaid diagram touched (one, in `docs/operations.md`) parses with
the real engine; the Stage 1 gate passes cumulatively across a restart
using this repository's established recovery-proof convention; performance
claims match the measurements printed above; and the single-operator
authority limit and TOCTOU residual window are stated precisely in
`docs/operations.md` without overclaiming.

`RELEASE PREP` remains **pending**. M17 begins only after this stack
(`m16/stabilize-01..04`) is externally merged alongside M1-M8 and M14a: no
milestone after M8 may compensate for a failed Stage 1 gate, and no
`v0.1.0` release preparation begins before that full set is merged.
