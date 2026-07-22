# Fault-Injection Recovery Demonstration Runbook

This is a reproducible procedure for the specific claim under test since the
original pitch memo: *for a supported provider operation, an interrupted or
retried run reconciles the persisted operation ID and provider state before
another side effect is attempted.* It walks through creating a real GitHub
issue, dispatching a real coding-agent worker, deliberately withholding
coordinator polling while that worker is still running, then continuing the
run and confirming no duplicate worker or duplicate pull request was ever
created.

A worked example — the exact commands and observations from one real,
recorded run — is published separately (see
[`README.md`](../README.md#recovery-demonstration) for the current link).
This document is the reusable procedure; the published write-up is one
dated execution of it.

## What "coordinator interruption" means here

Every `enginery stage1` command is a fresh, short-lived process; nothing
stays resident between invocations (see
[`docs/operations.md`](operations.md#recovery-semantics)). There is no
separate "recovery mode" to trigger — restarting the process and re-running
`stage1 watch --advance` against the same `--database` file *is* recovery.
A coding-agent worker launched by `stage1 watch --advance` runs as an
independent, detached OS process (its own session, its own process group)
that outlives the CLI invocation that launched it. "Interrupting the
coordinator" is therefore just *not calling `watch --advance` for a while*
— there is no coordinator to kill, because none persists between calls.
The interesting property under test is what a later, separate invocation
does when it finds a worker still running: it must recognize the durable
attempt and return `wait`, never launch a second worker.

## Prerequisites

- An authenticated `gh` CLI session with write access to a real or
  disposable repository you control.
- Either the `omp` or `claude` CLI installed and authenticated.
- `enginery` installed (`pip install enginery` or a source checkout).
- No product-specific credential beyond the above — Enginery never reads,
  stores, or requests a literal secret value; every credential field in a
  request is an opaque reference the GitHub/harness CLI resolves itself
  (see [`docs/operations.md`](operations.md#configuration)).

## Procedure

1. **Create a real, low-risk issue** in your target repository. Keep the
   acceptance criteria simple — a single pure function is enough to
   exercise the full lifecycle quickly. Record the issue number.

2. **Compose the request.** `enginery stage1 build-request` needs the
   repository, an `owner/repo#issue_number`-shaped `--external-reference`
   (not a full issue URL — that is a request-composition detail this
   demonstration's own worked example got wrong on a first attempt; see the
   published write-up), a `--repository-path` pointing at a local clone,
   fresh `--workspace-path`/`--artifact-root` directories, and
   `--harness-provider omp` or `claude-code`. Pass exactly one
   `--acceptance-criterion` — a live GitHub-sourced work item always
   derives its acceptance criteria as the issue body as a single item, so
   any locally-declared criteria are only placeholders for a real GitHub
   run; do not pass more than one or `qualify` will reject the mismatch.
   See [`docs/operations.md`](operations.md#composing-a-request) for the
   full flag reference.

3. **Start the run:**

   ```bash
   uv run enginery stage1 start --database ledger.db --owner operator --request request.json
   ```

4. **Advance to qualification, then to implementation dispatch.** Each
   call performs at most one durable action:

   ```bash
   uv run enginery stage1 watch --database ledger.db --owner operator --run-id <run-id> --advance
   # first call: qualify
   uv run enginery stage1 watch --database ledger.db --owner operator --run-id <run-id> --advance
   # second call: implement — this dispatches the worker and returns almost
   # immediately; the worker keeps running after this process exits
   ```

5. **Confirm a real, independent worker process exists.** Immediately
   after the dispatch call returns, find the worker in the process table
   (its parent PID is typically `1` — reparented after the launching CLI
   process exited):

   ```bash
   pgrep -fl enginery.engine.omp_worker
   ```

6. **This is the interruption: do nothing.** Do not call `watch` again for
   a real interval (tens of seconds is enough for a small task). Confirm
   the worker is still alive partway through:

   ```bash
   ps -p <pid> -o pid,ppid,etime,command
   ```

7. **Recover.** Call `watch --advance` again — a separate process
   invocation, the closest analog to a "replacement coordinator" this
   single-shot CLI has. Confirm the JSON result reports
   `"action_taken": "wait"` (not a second `"implement"` dispatch), and that
   `pgrep -fl enginery.engine.omp_worker` still shows exactly the same PID
   as step 5 — proof no second worker was launched while the first was
   still live:

   ```bash
   uv run enginery stage1 watch --database ledger.db --owner operator --run-id <run-id> --advance
   pgrep -fl enginery.engine.omp_worker
   ```

8. **Continue advancing** until the worker's result file appears and the
   run collects it (`"action_taken": "collect_implementation"`), then
   continues through `validate`, a real human `stage1 review`, `open_pr`,
   `wait_for_ci`, and `verify`:

   ```bash
   uv run enginery stage1 watch --database ledger.db --owner operator --run-id <run-id> --advance
   # repeat until next_action is await_human_review
   uv run enginery stage1 review --database ledger.db --owner operator --run-id <run-id> \
     --report review.json --repair-attempt 0
   uv run enginery stage1 watch --database ledger.db --owner operator --run-id <run-id> --advance
   # repeat until next_action is wait
   uv run enginery stage1 evidence --database ledger.db --owner operator --run-id <run-id>
   ```

9. **Check for duplicate side effects.** Confirm there is exactly one pull
   request against the target repository for this run's head branch, and
   exactly one commit on it:

   ```bash
   gh pr list --repo <owner>/<repo> --search "head:<head-branch>" --json number,url,state
   ```

## Publishing

Record the command transcript, timestamps, the worker PID and its
liveness check, the `action_taken`/`next_action` values from every
`watch --advance` call, and the final pull-request/evidence state.
Redact nothing except literal credential values (which should never
appear, since every credential in a request is an opaque reference, not a
secret — see Prerequisites above). Publish the write-up somewhere reachable
without special repository access, and link it from this repository's
`README.md`.
