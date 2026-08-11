# Enginery `v0.5.0` Release Notes

**`v0.5.0` makes M24's measurable, source-bound G4 evidence surface available to clean installs. It adds no workflow stage.** Stage 1 (issue to merge-ready pull request), Stage 2 (plan to verified release), and Stage 3 (incident to hotfix and rollback) remain the shipped workflow surface.

## What ships in `v0.5.0`

- **Source-bound work classification.** GitHub-sourced Stage 1 items retain their source snapshot and accept the canonical `enginery/work-kind/{issue,plan}` and `enginery/risk/{low,medium}` label vocabulary. Direct Stage 1 eligibility remains limited to low-risk `issue` and `plan` work.
- **Medium-risk human approval.** A classified medium-risk item reaches a real human-approval path; it does not proceed autonomously.
- **G4 deficiency evidence recording.** `enginery gate record-g4-deficiency` records a durable, source-bound recurring deficiency finding. `enginery gate record-g4-deficiency-evidence` verifies the referenced GitHub evidence pull request against configured numeric human identities.
- **Installed fail-closed G4 configuration.** `enginery gate status --gate G4 --database PATH --json` uses the packaged empty floor and principal roster for a clean consumer. It reports missing evidence as `unmeasured` or `fail`, never as a pass. Operators can supply an explicit `--floor-config PATH` with human-reviewed floors and principals.

## Compatibility and known limitations

- `v0.5.0` publishes schema and API versions but makes **no `1.0` stability promise**. Runs bind adapter and configuration fingerprints and block silent resume under changed behavior.
- This release adds **no workflow stage**. Stage 1, Stage 2, and Stage 3 are the complete shipped workflow surface.
- **G4 remains fail-closed.** It requires genuine multi-repository, dual-human numeric-identity, intervention, outcome, and recurring deficiency evidence before M14b/M15 work can begin.
- **Self-improvement is not implemented.** Stage 4 (governed factory self-improvement), cohort/replay, candidate evaluation, canary rollout, and promotion are not released capabilities.
- Repository-only documentation currency, test tooling, and published-evidence work are intentionally not part of this distribution.
- There is no hosted or multi-tenant service, organization RBAC, browser dashboard, or interactive TUI.
- **Windows is not supported.** Process supervision, cancellation, and recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace separation, not hostile-code containment.

## Installation

```bash
uv tool install enginery==0.5.0
enginery --version
enginery doctor
```

Requires Python 3.12+. Supports macOS and Linux.

## Links

- [`CHANGELOG.md`](CHANGELOG.md)
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)
- [`docs/operations.md`](docs/operations.md)
- [`docs/RELEASE_EVIDENCE.md`](docs/RELEASE_EVIDENCE.md)
