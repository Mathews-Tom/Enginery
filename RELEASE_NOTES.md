# Enginery `v0.8.0` Release Notes

## Breaking changes and migration

Python source imports are renamed by responsibility, without compatibility aliases. Re-import from the new modules and rebuild integrations or worker images that embed the old source paths before upgrading:

| Previous import | Use in 0.8.0 |
|---|---|
| `enginery.workflows.stage1` | `enginery.workflows.run_lifecycle` |
| `enginery.workflows.stage1_runtime` | `enginery.workflows.node_executors` |
| `enginery.cli.stage1` | `enginery.cli.engine_runs` |
| `enginery.cli.stage1_request` | `enginery.cli.request_compiler` |
| `enginery.cli.stage1_composition` | `enginery.cli.run_composition` |
| `enginery.cli.stage1_runner` | `enginery.cli.runner_wiring` |
| `enginery.cli.stage1_execution` | `enginery.cli.execution_diagnostics` |
| `enginery.cli.stage2` | `enginery.cli.stack_status` |

Class and function names, CLI commands/options, JSON and exit-code contracts, persisted `stage1` identifiers and request digests are unchanged by these renames. Existing runs do not require rebuilding solely because of the import-path change. New self-development requests must select the isolated runner; rebuild a trusted-local request with `enginery engine build-request --runner isolated-oci` rather than attempting to bypass the guard. Enginery does not promise `1.0` API/schema stability.

## What's new

### Isolated self-development and checkout execution

Enginery's own source project requires isolated execution, including advancement of already-bound runs through `run`, `continue` and `engine next --advance`. The installed runtime, operator configuration, ledger and credentials stay outside worker mounts. Trusted-local admission refuses unreadable, malformed or oversized existing `pyproject.toml` files with a project-identity diagnostic; isolated admission does not inspect that metadata.

The pinned worker image now includes CPython 3.12 and Enginery's hash-locked runtime dependencies alongside Debian Python 3.11.2. Rebuild the worker image to obtain these changes. Its Python-shebang `enginery-checkout` command executes the candidate's `./src/enginery` for network-free isolated validation and behavioral acceptance without installing or modifying the checkout. The wheel includes the checkout entry point and runtime-requirements OCI assets. Enginery's repository now supplies its own `.enginery/project.toml` and acceptance check.

### Runtime upgrade and rollback tooling

The repository-only standalone `scripts/enginery_runtime.py` launcher installs explicitly confirmed wheel hashes into retained virtual environments, lists runtime receipts and switches an external shim. Rollback checks every discovered or explicitly supplied ledger against the previous runtime's schema. Refusals identify the previous executable's restore command; an unreadable-ledger override requires operator pause/drain. Follow the pause/drain, backup, upgrade and restore procedures in [the operator guide](docs/operations.md). The launcher and documentation are not installed console commands.

### Bounded review repairs and publication leases

Before publication, actionable independent review findings and redacted `enginery feedback` dispatch the next bounded implementation repair generation with finding text in the worker task. Review context is deduplicated and capped at 32 KiB with an explicit truncation marker; feedback retains its 1 MiB input limit. Exhausted budgets and blocking findings still await human review, and publication authority is unchanged.

Repair publication expects the last earlier generation actually published according to the durable ledger, or an absent branch when review repairs precede the first publication. Unpublished intermediate generations no longer cause false remote conflicts; the publication lease still protects unexpected remote revisions.

### Read-only evidence integrity verification

`enginery engine evidence --run-id R --verify` re-hashes every artifact referenced by a run and reports intact, missing or tampered artifacts. Intact evidence exits 0; integrity failures use `validation_failure` (exit 12). A missing database raises a missing-prerequisite error without creating it. Verification does not mutate evidence or substitute integrity for an evaluated behavioral verdict.

## Explicitly not shipped

- No new workflow stage, G4 passage or Stage 4 implementation. G4 remains deferred/fail-closed; Stage 4 has **no committed date**.
- No hosted or multi-tenant service, organization RBAC, browser dashboard, interactive TUI, scheduled collection or unattended publication authority. Merge, release and deployment remain separate decisions.
- No claim that synthetic qualification proves usefulness on real projects, or that all platform/engine combinations are qualified. Both harness tiers remain experimental; effective effort remains unknown.
- No installed runtime-launcher console command and no compatibility aliases for renamed Python modules. Trusted-local execution is not hostile-code containment.
- No dependency changes since `v0.7.0`. Runtime, optional-extra and development dependency requirements are unchanged; the added worker assets do not change the package dependency graph.

## Compatibility and installation

Python 3.12+, macOS and Linux remain supported; Windows is unsupported because supervision and cancellation rely on POSIX process groups and signals. Stages 1–3 remain the workflow surface.

After publication, install the pinned version with:

```bash
uv tool install enginery==0.8.0
enginery --version
enginery doctor
```

Publication receipts and published-artifact consumer results belong to separate post-publication evidence, not these preparation notes.

## Links

- [Changelog](CHANGELOG.md)
- [Dependency manifest](docs/DEPENDENCIES.md)
- [Operator guide](docs/operations.md)
- [Release evidence](docs/RELEASE_EVIDENCE.md)
