# Enginery `v0.9.0` Release Notes

## Breaking changes and migration

The run request and its codec move to their own module, without compatibility aliases. Update Python imports before upgrading:

| Name | Previous import | Use in 0.9.0 |
|---|---|---|
| `Stage1RunRequest` | `enginery.workflows.run_lifecycle` | `enginery.workflows.run_intent` |
| `Stage1ExecutionConfiguration` | `enginery.workflows.run_lifecycle` | `enginery.workflows.run_intent` |
| `Stage1ImplementationRequest` | `enginery.workflows.run_lifecycle` | `enginery.workflows.run_intent` |
| `Stage1Run` | `enginery.workflows.run_lifecycle` | `enginery.workflows.run_intent` |
| `stage1_request_from_state` | `enginery.workflows.run_lifecycle` | `enginery.workflows.run_intent` |

`Stage1RunService`, `Stage1ImplementationDispatch` and `implementation_attempt_id` stay in `enginery.workflows.run_lifecycle`. There are no aliases for the moved imports. Class and function names, CLI commands/options, JSON and exit-code contracts, persisted `stage1` identifiers and request digests are unchanged by the import-path move. Existing runs need no rebuild solely because of this move. Enginery does not promise `1.0` API/schema stability.

## What's new

### Fixed

`enginery engine evidence --verify` rejects a node whose `artifact_references` is present but not a list, including `""`, `0`, `false` and `null`, as `validation_failure` (exit 12); only an absent field means no references. A ledger that SQLite cannot read read-only, such as a WAL ledger in a non-writable directory, exits 3 (missing prerequisite) with a named error instead of a `sqlite3` traceback. Verification remains read-only and does not substitute integrity for an evaluated behavioral verdict.

## Explicitly not shipped

- No new workflow stage, G4 passage or Stage 4 implementation. G4 remains deferred/fail-closed; Stage 4 has **no committed date**.
- No hosted or multi-tenant service, organization RBAC, browser dashboard, interactive TUI, scheduled collection or unattended publication authority. Merge, release and deployment remain separate decisions.
- No claim that synthetic qualification proves usefulness on real projects, or that all platform/engine combinations are qualified. Both harness tiers remain experimental; effective effort remains unknown.
- No compatibility aliases for the moved Python imports and no installed runtime-launcher console command. Trusted-local execution is not hostile-code containment.
- No dependency changes since `v0.8.0`. Runtime, optional-extra and development dependency requirements are unchanged.

## Compatibility and installation

Python 3.12+, macOS and Linux remain supported; Windows is unsupported because supervision and cancellation rely on POSIX process groups and signals. Stages 1–3 remain the workflow surface.

After publication, install the pinned version with:

```bash
uv tool install enginery==0.9.0
enginery --version
enginery doctor
```

Publication receipts and published-artifact consumer results belong to separate post-publication evidence, not these preparation notes.

## Links

- [Changelog](CHANGELOG.md)
- [Dependency manifest](docs/DEPENDENCIES.md)
- [Operator guide](docs/operations.md)
- [Release evidence](docs/RELEASE_EVIDENCE.md)
