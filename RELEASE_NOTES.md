# Enginery `v0.10.0` Release Notes

## Breaking changes and migration

None. No CLI commands/options or Python import paths are removed or renamed relative to `v0.9.0`. Invalid non-finite drain/relay waits are now refused as described below; zero and positive finite waits retain their behavior. Existing runs need no schema migration or rebuild for these additions. Enginery does not promise `1.0` API/schema stability.

## What's new

### Added

- `enginery outcome summary --database PATH --run-id RUN` emits a read-only, evidence-backed JSON scorecard for one Stage 1 run. It separates native publication and merge readiness from downstream outcomes, deduplicates resumed attempts, and reports retained lifecycle facts, recorded assistance and measurement coverage. Missing measurements and external human effort are unknown, not zero; the command neither advances the run nor calls a provider.
- OMP implementation attempts retain harness-reported tokens and cost components from finalized assistant messages in the existing fenced attempt result, for direct and supervised collection and for failed as well as successful attempts. The scorecard exposes these per attempt under `measurements.usage`, with source-artifact evidence, message counts, counted lines, `harness`/`worker` attribution and explicit coverage. These are harness/worker claims, not independently verified billing. No currency or run totals are inferred. Streaming snapshots and repeated collection do not inflate counts; missing or malformed reports remain explicit rather than becoming zero.

### Fixed

- Review packets no longer mistake an unquoted call such as `token = check_token_file(...)` in unchanged source context for a generic secret assignment. Credential-shaped literals and known token prefixes remain refused; scanning string leaves before JSON escaping also prevents quoted credentials from bypassing refusal. Head/evidence bindings and terminal approval protections are unchanged.
- `enginery operator drain --wait-seconds` rejects non-finite (`nan`, `inf`, `-inf`) and negative durations with exit 2 and a JSON `invalid_input` error before pausing dispatch. Previously `nan` and `inf` were accepted and paused dispatch. `enginery runner relay up --wait-seconds` likewise rejects non-finite and negative waits before any engine call; zero and positive finite waits are unchanged.
- Enginery's factory validation runs offline Ruff lint and format checks and an operator-drain acceptance smoke instead of only `enginery-checkout engine --help`. The locked Ruff version is baked hash-pinned into the runner image. The drain observer is bound in the acceptance configuration; failed checks are not skipped.

## Explicitly not shipped

- No live-pilot evidence that OL2's review-packet fix restores native publication end to end. The credential false-positive fix is not proof of autonomous delivery.
- No timing or coordination attribution, complete human-effort accounting, verified provider billing, inferred currency or run-level usage/cost totals.
- No Claude Code usage producer and no historical usage backfill. Earlier records and other harnesses remain explicitly unmeasured.
- No autonomous optimization or routing based on the new scorecard or reported usage.
- No new workflow stage, G4 passage or Stage 4 implementation. Stage 4/G4 remains deferred/fail-closed with **no committed date**.
- No hosted or multi-tenant service, organization RBAC, browser dashboard, interactive TUI, scheduled collection or unattended publication authority. Merge, release and deployment remain separate decisions.
- No claim that synthetic qualification proves usefulness on real projects, or that all platform/engine combinations are qualified. Both harness tiers remain experimental; effective effort remains unknown. Trusted-local execution is not hostile-code containment.
- No dependency changes since `v0.9.0`. Runtime, optional-extra and development dependency requirements and lock resolutions are unchanged; the isolated runner image now includes the already-locked Ruff for offline validation.

## Compatibility and installation

Python 3.12+, macOS and Linux remain supported; Windows is unsupported because supervision and cancellation rely on POSIX process groups and signals. Stages 1–3 remain the workflow surface.

After publication, install the pinned version with:

```bash
uv tool install enginery==0.10.0
enginery --version
enginery doctor
```

Publication receipts and published-artifact consumer results belong to separate post-publication evidence, not these preparation notes.

## Links

- [Changelog](CHANGELOG.md)
- [Dependency manifest](docs/DEPENDENCIES.md)
- [Operator guide](docs/operations.md)
- [Release evidence](docs/RELEASE_EVIDENCE.md)
