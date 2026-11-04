# Enginery `v0.10.0` Release Notes

## Breaking changes and migration

No CLI commands/options or Python import paths are removed or renamed relative to `v0.9.0`, and no schema migration is needed. Invalid non-finite drain/relay waits are now refused as described below; zero and positive finite waits retain their behavior. Enginery does not promise `1.0` API/schema stability.

**Rebuild the isolated worker image.** The worker recipe now includes the locked Ruff for offline validation (see below), so its recipe digest changes. After upgrading, a worker image built by `v0.9.0` or earlier is stale: run `enginery runner build-image`, qualify it, and bind new isolated runs to the new image. An isolated run already bound to an older image is held as `runner_fingerprint_drift` rather than launched on a different image. The relay image recipe is unchanged.

## What's new

### Added

- `enginery outcome summary --database PATH --run-id RUN` emits a read-only, evidence-backed JSON scorecard for one Stage 1 run. It separates native publication and merge readiness from downstream outcomes, deduplicates resumed attempts, and reports retained lifecycle facts, recorded assistance and measurement coverage. Missing measurements and external human effort are unknown, not zero; the command neither advances the run nor calls a provider.
- OMP implementation attempts retain harness-reported tokens and cost components from finalized assistant messages in the existing fenced attempt result, for direct and supervised collection and for failed as well as successful attempts. The scorecard exposes these per attempt under `measurements.usage`, with source-artifact evidence, message counts, counted lines, `harness`/`worker` attribution and explicit coverage. These are harness/worker claims, not independently verified billing. No currency or run totals are inferred. Streaming snapshots and repeated collection do not inflate counts; missing or malformed reports remain explicit rather than becoming zero.

### Fixed

- The shared credential-shaped detector no longer treats an unquoted call such as `token = check_token_file(...)` as a generic secret assignment. This fixes the review-packet refusal that blocked native publication. The same narrowing applies everywhere that detector redacts output or refuses ledger and artifact writes. Quoted and unquoted credential-shaped literals and known token prefixes remain redacted and refused. Packet string leaves are scanned before JSON escaping, so double-quoted credentials cannot bypass refusal. Head/evidence bindings and terminal approval protections are unchanged.
- `enginery operator drain --wait-seconds` rejects non-finite (`nan`, `inf`, and `-inf` written as `--wait-seconds=-inf`) and negative durations with exit 2 and a JSON `invalid_input` error before pausing dispatch. Previously `nan` and `inf` were accepted and paused dispatch. A space-separated `--wait-seconds -inf` is parsed as an option and refused by argument parsing, also with exit 2. `enginery runner relay up --wait-seconds` likewise rejects non-finite and negative waits before any engine call; zero and positive finite waits are unchanged.
- Enginery's factory validation runs offline Ruff lint and format checks and an operator-drain acceptance smoke instead of only `enginery-checkout engine --help`. The locked Ruff version is baked hash-pinned into the worker image, which therefore needs a rebuild (see above). The drain observer is bound in the acceptance configuration; failed checks are not skipped.

## Explicitly not shipped

- No live-pilot evidence that OL2's review-packet fix restores native publication end to end. The credential false-positive fix is not proof of autonomous delivery.
- No timing or coordination attribution, complete human-effort accounting, verified provider billing, inferred currency or run-level usage/cost totals.
- No Claude Code usage producer and no historical usage backfill. Earlier records and other harnesses remain explicitly unmeasured.
- No autonomous optimization or routing based on the new scorecard or reported usage.
- No new workflow stage, G4 passage or Stage 4 implementation. Stage 4/G4 remains deferred/fail-closed with **no committed date**.
- No hosted or multi-tenant service, organization RBAC, browser dashboard, interactive TUI, scheduled collection or unattended publication authority. Merge, release and deployment remain separate decisions.
- No claim that synthetic qualification proves usefulness on real projects, or that all platform/engine combinations are qualified. Both harness tiers remain experimental; effective effort remains unknown. Trusted-local execution is not hostile-code containment.
- No dependency changes since `v0.9.0`. Runtime, optional-extra and development dependency requirements and lock resolutions are unchanged; the isolated worker image now includes the already-locked Ruff for offline validation, which changes its recipe digest.

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
