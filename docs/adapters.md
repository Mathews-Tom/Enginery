# Adapter Authoring Guide

Enginery's core (`domain`, `application`, `engine`, `policy`, `evidence`,
`evaluation`) never imports a provider SDK and never sees a provider-native
object. Every external system — a work ledger, an agent harness, a
workspace backend, source control, validation, release/deployment, or a
capability registry — is reached through one of the typed `Protocol`
interfaces ("ports") in `src/enginery/application/`. This is the contract a
new adapter must satisfy, grounded directly in the real port definitions
and the two shipped harness adapters (`OmpHarness`, `ClaudeCodeHarness`) as
worked reference implementations.

## Package boundary

```text
domain/       Immutable domain types, state transitions, schemas, invariants
application/  Use cases and ports; coordinates domain behavior
engine/       Workflow scheduling, process managers, leases, recovery
ledger/       SQLite event store, projections, inbox, outbox, migrations
policy/       Action schemas, hard rules, approval digests, decisions
evidence/     Evidence contracts, verification, terminal claims
evaluation/   Outcomes, cohorts, replay, comparison, canaries
adapters/     Provider implementations and normalization boundaries
cli/          Commands, JSON output, JSONL streaming, exit codes
```

`domain` imports nothing else in this repository. `application` depends
inward on `domain` and outward only through declared ports. `engine`,
`policy`, `evidence`, and `evaluation` never import provider SDKs.
Provider-native objects enter and leave **only** through `adapters`. `cli`
owns presentation and invokes application services; it never implements a
domain transition directly. Verify a layer at any time with:

```bash
uv run python scripts/check_import_boundaries.py <layer>
```

## The port catalog

| Port (Protocol) | Module | First implementations | Required methods |
|---|---|---|---|
| `WorkLedgerPort` | `application/work_ports.py` | `LocalWorkLedger`, `GitHubWorkLedger` | `probe`, `fetch`, `publish_lifecycle`, `reconcile` |
| `HarnessPort` | `application/work_ports.py` | `OmpHarness`, `ClaudeCodeHarness`, `ScriptedHarness` | `probe`, `start`, `events`, `result`, `cancel`, `reconcile` |
| `WorkspacePort` | `application/work_ports.py` | `LocalWorkspace` (`local-git-worktree`) | `probe`, `create`, `retain`, `cleanup`, `reconcile` |
| `SourceControlPort` | `application/work_ports.py` | `LocalGit` (`local-git`) | resolve/change/branch/commit operations plus `probe`, `reconcile` |
| `PullRequestPort` | `application/work_ports.py` | `GitHubPullRequests` | `probe`, `create_or_update`, `get`, `evidence`, `merge`, `reconcile` |
| `ValidationPort` | `application/delivery_ports.py` | `LocalValidation` (`local-validation`) | `probe`, `validate`, `reconcile` |
| `ReleasePort` | `application/delivery_ports.py` | `LocalPublication`, `GitHubReleaseAdapter`, `PyPiAdapter` | `probe`, `publish`, `verify`, `reconcile` |
| `DeploymentPort` | `application/delivery_ports.py` | `LocalDeploymentFixture` | `probe`, `deploy`, `rollback`, `reconcile` |
| `CapabilitySourcePort` | `application/delivery_ports.py` | `LocalCapabilitySource`, `ArmoryCapabilitySource` | `probe`, `discover`, `resolve`, `fetch` |

Every port shares the same identity/reconciliation shape, described next.

## Every adapter must

Per the system design's general adapter requirements:

1. **Expose identity, version, and capabilities.** `probe()` returns an
   `AdapterStatus` (`kind`, `availability`, `fingerprint`, `detail`). An
   `AVAILABLE` status *requires* a non-`None` `AdapterFingerprint`
   (`provider_id`, `provider_version`, `api_version` — currently
   `ADAPTER_API_VERSION = 1`, `capabilities`); an `UNAVAILABLE` or
   `MISCONFIGURED` status *requires* `fingerprint=None`. These are enforced
   invariants, not conventions — constructing an inconsistent `AdapterStatus`
   raises `InvalidInputError`.
2. **Validate configuration before use.** Adapter configuration dataclasses
   (`GitHubAdapterConfig`, `OmpAdapterConfig`, `ClaudeCodeAdapterConfig`,
   and so on) validate every field in `__post_init__` — a blank credential
   reference or executable name fails immediately, not on first use.
3. **Normalize provider data at the boundary.** Provider-native objects
   (a `gh` JSON payload, an OMP event line, a Claude Code `stream-json`
   record) are mapped into the port's own types
   (`WorkLedgerSnapshot`, `PullRequestSnapshot`, `NormalizedAdapterEvent`,
   and so on) inside the adapter module. Nothing provider-shaped crosses
   into `application`, `engine`, `policy`, `evidence`, or `evaluation`.
4. **Receive a persisted operation ID for each side effect** and implement
   the four-result reconciliation query: `reconcile(operation_id=...)`
   returns one of `ReconciliationResult.FOUND_MATCHING`, `NOT_FOUND`,
   `FOUND_CONFLICTING`, or `INDETERMINATE`. This is what lets the
   coordinator recover from an interruption without duplicating or losing
   a side effect (see [`docs/operations.md`](operations.md#recovery-semantics)).
5. **Classify failures** as permanent, transient, policy, authentication,
   rate-limit, conflict, or ambiguous (`enginery.domain.errors`'s
   `FailureClass` values) rather than letting a raw provider exception
   escape the adapter boundary unclassified.
6. **Emit redacted diagnostic events.** Harness output in particular is
   untrusted: `redact_credential_shaped_text` runs before any output is
   persisted, and a `HarnessOutput`/artifact carries a `RedactionClassification`.
7. **Provide focused contract tests** — see `tests/adapters/` for the
   pattern each shipped adapter follows, and the shared parametrized
   harness fixture described below for the two-harness case specifically.
8. **Never hide a fallback to another provider.** An unconfigured or
   unavailable adapter is a diagnostic failure the operator must resolve,
   never a silent substitution for a different one.

Do not add a general plugin/discovery framework before two real
implementations require the same extension point.

## Two independent harness adapters, side by side

`OmpHarness` and `ClaudeCodeHarness` (`src/enginery/adapters/omp.py`,
`src/enginery/adapters/claude_code.py`) are the reference pair for
"what a correctly shaped adapter pair looks like." Neither is mandatory or
a fallback for the other — a run's `execution_configuration` names exactly
one `harness_provider` (`"omp"` or `"claude-code"`); an unconfigured or
missing provider is a diagnostic failure. Both adapters:

- probe by running the CLI's own version command and report
  `AdapterAvailability.UNAVAILABLE` (CLI absent) or `MISCONFIGURED`
  (unexpected output) with `fingerprint=None`, never a fallback;
- normalize their own event schema into the same
  `NormalizedAdapterEvent` kinds (`STARTED`/`PROGRESS`/`DIAGNOSTIC`/`TERMINAL`),
  rejecting unknown or malformed lines as a classified failure rather than
  skipping them;
- redact output before artifact publication;
- have no CLI-level timeout flag — the coordinator's lease/heartbeat
  expiry calls the same cancellation entry point an operator would, so a
  timeout and an operator cancellation are the same code path and the same
  reported outcome;
- run through the same coordinator-supervised dispatch path;
- report harness/model metadata from observed output, never a hardcoded
  model ID.

A shared parametrized fixture (`tests/adapters/`) exercises
unavailable-harness diagnostics, malformed-output rejection, a clean run's
terminal status, and cancellation identically against both adapters, and
asserts that neither the shared task envelope (`HarnessTask`) nor the
shared running-worker identity type (`HarnessSession`) carries a
provider-named field. A third harness adapter should pass the same shared
fixture before being considered complete.

## Worked example: writing a new `WorkLedgerPort` adapter

Sketch (eliding validation and error handling already covered above; see
`src/enginery/adapters/github.py`'s `GitHubWorkLedger` for the complete,
real version):

```python
from enginery.application.adapter_types import (
    AdapterAvailability, AdapterFingerprint, AdapterStatus, ProviderKind,
)
from enginery.application.work_ports import WorkLedgerPort, WorkLedgerSnapshot, LifecycleProjection
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

class MyTrackerWorkLedger:
    """A WorkLedgerPort implementation for a new issue tracker."""

    def probe(self) -> AdapterStatus:
        # Run the tracker CLI/SDK's own version check; never assume availability.
        ...
        return AdapterStatus(
            kind=ProviderKind.WORK_LEDGER,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="my-tracker", provider_version="1.4.0", api_version=1,
            ),
            detail="my-tracker is available",
        )

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        # Fetch, then map provider-native fields into WorkItem/WorkLedgerSnapshot.
        # Provider-specific labels/states/users are mapped here, not exposed upward.
        ...

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        # Publish idempotently under operation_id; return FOUND_MATCHING on success.
        ...

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        # Query deterministic provider markers for the given operation_id.
        ...
```

This satisfies `WorkLedgerPort` structurally (Python `Protocol` types are
duck-typed — no base-class inheritance is required) and mirrors the shape
every other port implementation follows.

## The Armory capability-registry relationship

Armory (`Mathews-Tom/armory`) is a catalog and distribution surface for
skills, agents, hooks, rules, commands, utilities, and presets — it equips
agents; Enginery directs engineering. Enginery never depends on Armory for
its domain model or execution kernel:

```text
Armory equips agents.
Enginery directs engineering.
Agent harnesses perform assigned work.
Repositories and delivery systems receive verified outcomes.
```

`ArmoryCapabilitySource` (`src/enginery/adapters/armory.py`) implements
`CapabilitySourcePort` against Armory's real, current interface: a single
`manifest.yaml` fetched over HTTPS, with no MCP server, no per-package
signature, and no checksum field of its own. Two facts about this adapter
matter operationally:

- **It is not a runtime dependency.** Importing the module needs only the
  standard library; every network-reaching method requires the optional
  `armory` extra (`uv sync --all-extras` installs it). `probe()` reports
  `AdapterAvailability.UNAVAILABLE` when the extra is absent — the engine
  keeps working with Armory disabled, exactly like an absent OMP or Claude
  Code CLI. **No shipped CLI command constructs or calls this adapter in
  this release** — using it today means writing Python against
  `CapabilitySourcePort` directly, the same way `tests/adapters/armory`
  exercises it.
- **Every capability it returns is `provenance="armory"`, which
  `CapabilityResolver` classifies as `unauthenticated`.** Content
  addressing (Enginery's own fetch-and-hash step) is not the same as
  trust: Armory supplies no cryptographic signature today, so every
  Armory-sourced capability a run introduces requires interactive
  exact-digest human approval (the `capability.materialize` policy action)
  before that run can execute it — the same hard rule that governs a
  repository-local capability added or changed by the active run. There is
  no "authenticated-by-TLS" fast path; TLS or transport identity alone is
  explicitly insufficient.

Repository-local capabilities (`LocalCapabilitySource`) remain valid
without any registry — "the engine works with Armory disabled" is an
enforced acceptance property, not an aspiration.

## Example workflows

See [`docs/examples.md`](examples.md) for two complete, runnable worked
examples: a fully local, no-live-provider Stage 1 request (the same
composition `scripts/full_system_gate.py` uses for the cumulative
restart/replay gate), and a real GitHub/OMP-backed request (the same shape
[`docs/migration-sage-dev.md`](migration-sage-dev.md) uses to bring a
historical ticket into Enginery).
