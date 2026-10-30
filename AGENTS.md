# AGENTS.md

## Code Review Rules

- Failure means a concrete regression in Enginery's local-first, governed engineering control plane:
  - `src/enginery/workflows/run_lifecycle.py`: recovery or continuation loses durable run identity, dispatches duplicate work, or publishes against the wrong reviewed head.
  - `src/enginery/policy/approval.py` or `src/enginery/cli/factory_decisions.py`: an expired, superseded, wrong-action, or wrong-digest approval authorizes an effect; Stage 1 merges, or publication/deployment/rollback bypasses required human authority.
  - `src/enginery/ledger/verify.py` or `src/enginery/cli/engine_runs.py`: missing, tampered, or malformed retained evidence is accepted as verified, or inspection silently repairs durable state.
  - `src/enginery/cli/` or `docs/operations.md`: documented commands/flags or exit codes break, or a command contradicts its stated mutation/model-call boundary; distinguish ledger-content changes from disclosed SQLite WAL sidecars.
- Do not report style, formatting, naming, or diagnostics already enforced by Ruff check/format, `mypy --strict src`, or `scripts/check_import_boundaries.py`; avoid speculative hardening beyond the documented trusted-local threat model.
- Limit actionable findings to regressions introduced by the PR. If unchanged issues must be mentioned for context, tag them Pre-existing without requesting a fix. Do not repeat findings raised on an earlier commit of the same PR.
- Skip routine review of `**/__pycache__/`. `uv.lock` stays in scope for dependency changes: flag packages added or version-changed without a matching `pyproject.toml` change; do not review it line by line.
- Every finding must name `file:line`, its bug class, and a concrete failing scenario supported by the changed code and relevant callers/contracts—not an inference from a name. Identify the shared cause so the author can fix every affected instance.
- Tag each finding Important / Nit / Pre-existing. Start the summary with the count of Important findings; when there is nothing actionable, say plainly “Nothing to fix.”
