# Enginery `v0.4.0` Release Notes

**`v0.4.0` makes the already-merged operator surface available to clean installs. It adds no workflow stage.** Stage 1 (issue to merge-ready pull request), Stage 2 (plan to verified release), and Stage 3 (incident to hotfix and rollback) remain the shipped workflow surface.

## What ships in `v0.4.0`

- **Gate G4 readiness reporting.** `enginery gate status --gate G4 --database PATH [--floor-config PATH] [--json]` reports each registered entry condition from the durable ledger and configured floors. Missing or insufficient evidence is reported as `unmeasured` or `fail`, never as a pass.
- **Guided Stage 1 request construction.** `enginery stage1 build-request` writes a validated request document for `enginery stage1 start` from explicit command-line inputs.
- **Workspace reservation operations.** `enginery workspace inspect` reports current durable workspace reservations. `enginery workspace release` uses the existing fenced proof before it releases a reservation, and supports `--dry-run`.
- **Stage 2 and Stage 3 broker visibility.** `enginery adapter doctor --json` includes the GitHub Release and PyPI publication brokers plus the controlled-local-service deployment broker. These probes check local prerequisites only; they do not publish, deploy, or make a network call.

## Compatibility and known limitations

- `v0.4.0` publishes schema and API versions but makes **no `1.0` stability promise**. Runs bind adapter and configuration fingerprints and block silent resume under changed behavior.
- This release adds **no workflow stage**. Stage 1, Stage 2, and Stage 3 are the complete shipped workflow surface.
- **Self-improvement is not implemented.** Stage 4 (governed factory self-improvement) is gate-deferred behind G4 with no committed date. Candidate evaluation, canary rollout, and promotion are not released capabilities.
- Repository-only documentation currency, test tooling, and published-evidence work (M20–M23) are intentionally not part of this distribution.
- There is no hosted or multi-tenant service, organization RBAC, browser dashboard, or interactive TUI.
- **Windows is not supported.** Process supervision, cancellation, and recovery are bound to POSIX process groups and signals.
- The worktree backend provides workspace separation, not hostile-code containment.

## Installation

```bash
pip install enginery==0.4.0
enginery --version
enginery doctor
```

Requires Python 3.12+. Supports macOS and Linux.

## Links

- [`CHANGELOG.md`](CHANGELOG.md)
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)
- [`docs/operations.md`](docs/operations.md)
- [`docs/RELEASE_EVIDENCE.md`](docs/RELEASE_EVIDENCE.md)
