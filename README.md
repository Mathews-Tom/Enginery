# Enginery

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Enginery is an open-source, local-first control plane for agentic
engineering. It coordinates humans, coding agents, and deterministic tools
through governed workflows that turn engineering intent into verified
software outcomes, and improve the system that produces them.

## Status

`v0.9.0` is the current published release (2026-10-30); `v0.1.0`…`v0.8.0` are also published on PyPI and GitHub Releases. `v0.9.0` moves the run request to its own module and makes `engine evidence --verify` reject malformed records. Enginery ships three of its four planned
workflow stages: a coordinator-owned issue-to-merge-ready-pull-request
workflow (Stage 1, proven against a real GitHub repository and a real
OMP/Claude Code coding-agent harness), a dependency-ordered
plan-to-verified-release workflow with a second coding-agent harness
and capability provenance (Stage 2, proven against a real disposable
PyPI/TestPyPI and GitHub Release publication), and a
production-incident-to-hotfix-and-rollback workflow (Stage 3, proven
against a real but controlled local HTTP service — never a real
production, container, or cloud destination). Stage 4 (governed
factory self-improvement) is gate-deferred with no committed date; run
`enginery gate status --gate G4 --database ledger.db` to see exactly which entry conditions
are currently met. See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) and
[`CHANGELOG.md`](CHANGELOG.md) for the full compatibility statement.

The CLI uses top-level project verbs, `engine` for engine-ledger operations, and
`operator` for verified recovery. Removed `stage1`, `stage2`, `ledger`, and unreleased
`factory` command paths have no aliases. Both harness tiers remain experimental;
live qualification against a designated synthetic qualification repository proves
factory mechanics, not usefulness on real projects.

### Guided setup and manual factory execution

`enginery init`, `preflight`, `explain`, `uninstall`, and `collect` are shipped: guided,
preview-first project setup, read-only prerequisite diagnostics, an explanation of where
every configured value comes from, and reversible removal of exactly what setup created,
described in [Guided project setup](docs/operations.md#guided-project-setup). They write
only a project policy file and a private ownership record, and never install
capabilities, sign in, or touch services, MCP servers or global configuration.

`enginery collect --dry-run` previews canonical issue classification, deduplication,
workflow selection and budgets without GitHub mutations. Read-only inspection of a WAL
ledger may create SQLite sidecars, disclosed in `local_writes`, without changing the
ledger's content. `collect` updates only the private
repository cursor, never issue-to-run bindings, and never skips held issues. JSON
reports GitHub authentication probes separately from issue reads; neither form calls
a model or changes GitHub.

`enginery run OWNER/REPO#N` compiles a bound request and advances the
existing Stage 1 coordinator, not a second scheduler. `status [RUN]`
reports durable progress and the run's immutable binding. `enginery explain`
explains current configuration and takes no run ID.
See the [Manual project path](docs/operations.md#manual-project-path) for private
state, run identity, overrides and holds. `review RUN` renders a safe local
terminal/HTML packet and issues a terminal-only one-time challenge to a registered
operator. `decide RUN --action ACTION --subject-digest DIGEST --challenge SECRET`
records authenticated, expiring action-specific authority; publication uses
`pull_request.open`. Feedback and Stage 1 review reports record findings only.
See [terminal decisions](docs/operations.md#authenticated-terminal-decisions)
for registration, node-scoped waits, supersession and trusted-local limitations.
GitHub readiness uses fully paginated, exact-head API evidence retained in the
private artifact store and bound into the terminal evidence bundle; incomplete
collections hold rather than accepting a prefix. Review/status reuse that evidence.
Optional `status RUN --github-view` uses user-configured `gh-axi` only
for escaped, read-only display, never evidence or approval. See
[GitHub evidence and display](docs/operations.md#authoritative-github-evidence-and-optional-display).

`enginery continue RUN` reconciles and continues the same durable run.
`enginery pause` pauses project-ledger dispatch; `enginery resume` lifts that
pause without continuing a run. `enginery drain` drains supervised workers.
`enginery calibrate` and `enginery check` run independent behavioral acceptance.
Inspect project progress with `enginery status RUN`; use `enginery engine evidence`
with an explicit database, owner and run ID for retained run evidence.
Scheduled collection is not shipped.
The shipped boundary is manual-first, local-first, and deterministic: models never
poll, schedule, keep books, or decide whether a check is complete; routine control
uses no model. Harness, model, session, and worker machine are separate axes.

### Licensing and security limits

No upstream skills, prompts, or code from reference projects are vendored;
any pattern learned from them is independently re-implemented under this
repository's Apache-2.0 license. Trusted-local operation is not containment:
a worker process can reach the operator's account, filesystem, network, and
keychain, and Enginery makes no claim of safety against hostile code.
What the isolated runner enforces, how it was qualified and what stays out of
scope is stated in
[Security limits and what is qualified](docs/operations.md#security-limits-and-what-is-qualified).
Stage 1 never merges. A Stage 2 pull-request merge is policy-gated: the
shipped default rules allow low- and medium-risk merges once current-head
merge-ready evidence passes, and deny high-risk merges by default. Release
publication, deployment, and rollback always require a current interactive
human approval. Enginery never triggers external reviewer bots.

## Recovery demonstration

A recorded, real coordinator-interruption-and-recovery run: a live
coding-agent worker dispatched against a real GitHub issue, deliberately
interrupted (no coordinator polling for 39 seconds while the worker kept
running as an independent process), then recovered by a later, separate
invocation that recognized the still-running worker and never dispatched
a second one — confirmed by exactly one pull request and one commit.
Published write-up:
[gist.github.com/Mathews-Tom/eb2ff07e918b329dc25a0fbfcab71945](https://gist.github.com/Mathews-Tom/eb2ff07e918b329dc25a0fbfcab71945).
The reusable runbook is [`docs/recovery-demonstration.md`](docs/recovery-demonstration.md).

## Pilot evidence and research

- [`docs/pitch.md`](docs/pitch.md) records the Stage 1 gate-G1 pilot
  (`go`, 2026-07-20) and a follow-on Stage 2/Stage 3
  manual-baseline-versus-Enginery comparison (2026-07-22) — real
  elapsed time, intervention counts, and burden for both paths, with
  no productivity claim beyond the recorded numbers.
- [`docs/competitive-capability-matrix.md`](docs/competitive-capability-matrix.md)
  is a dated, hands-on capability comparison against the closest
  control-plane entrants, with a first-hand observation or an explicit
  "not independently verified" mark for every row.

## Installation

```bash
pip install enginery
```

Or from source:

```bash
git clone https://github.com/Mathews-Tom/Enginery.git
cd Enginery
uv sync --all-extras --dev
```

## Quick start

The usual path takes one GitHub issue in your project to a verified, merge-ready pull request. You keep the publication decision and the merge.

1. Install Enginery and run `enginery doctor`.
2. In the project, preview setup with `enginery init --runner trusted-local`, or with `enginery init --runner isolated-oci --runner-image sha256:IMAGE_ID` using the id printed by `enginery runner build-image` (or set `image` under `[runner]` in user preferences). Then add `--apply`. Pass `--validation-command` unless the project policy already defines `[validation] commands`. `isolated-oci` is required for Enginery's own repository.
3. Commit nonempty `[acceptance]` checks to `.enginery/project.toml` on the base branch. A run without them holds with `no_behavioral_checks`.
4. Register yourself under `[review_authority]` in `~/.config/enginery/user.toml` so `review` can issue you decision challenges.
5. Run `enginery preflight`, then `enginery run OWNER/REPO#N`. The worker implements the issue, and Enginery validates and checks the result.
6. Run `enginery review RUN` in a real terminal, then `enginery decide RUN --action pull_request.open --subject-digest DIGEST --challenge SECRET` to allow publication. Send repair findings with `enginery feedback` instead if the candidate needs work.
7. Run `enginery continue RUN` until `status` reports the verify node passed (CI green and merge-ready on the current head). Merge the PR yourself; Stage 1 never merges.
8. Inspect the run's retained evidence with `enginery engine evidence --database DB --owner factory --run-id RUN --verify`. `status RUN` prints the project ledger path.

The [Manual project path](docs/operations.md#manual-project-path) and [Guided project setup](docs/operations.md#guided-project-setup) cover each step, its holds and its repairs.

## CLI

Project commands use the current directory by default (`--project` selects another
project). Host diagnostics do not replace project prerequisite checks.

```bash
uv run enginery --version
uv run enginery doctor                                     # host diagnostics
uv run enginery init --runner trusted-local                # preview; add --apply to write
uv run enginery init --runner trusted-local --apply        # after reviewing preview
uv run enginery preflight                                  # project prerequisites
uv run enginery explain
uv run enginery collect --dry-run
uv run enginery run OWNER/REPO#N --once
uv run enginery status RUN
uv run enginery check RUN                                  # behavioral acceptance verdict
uv run enginery calibrate RUN                              # deliberate-fault calibration
uv run enginery review RUN                                 # safe packet + terminal challenge
uv run enginery decide RUN --action pull_request.open --subject-digest DIGEST --challenge SECRET
uv run enginery feedback RUN --subject-digest DIGEST --text "repair finding"
uv run enginery continue RUN                               # reconcile/advance same run
uv run enginery pause --reason "operator maintenance"
uv run enginery resume                                     # lift project dispatch pause
uv run enginery drain --wait-seconds 30                    # or --stop
uv run enginery uninstall                                  # preview; add --apply after review
```

Lower-level host, engine, operator, policy and capability commands are shown
below. Ledger-scoped commands select a database explicitly; engine run and stack
commands also select their coordinator owner. `engine build-request` writes a
request document without selecting a ledger or owner:

```bash
uv run enginery adapter doctor --json
uv run enginery operator verify --database ledger.db
uv run enginery policy explain request.json
uv run enginery engine build-request --runner trusted-local \
  --output request.json --run-id issue-1 \
  --repository owner/repo --external-reference "owner/repo#1" \
  --base-revision "..." --repository-path /path/to/repo \
  --workspace-path /path/to/workspace \
  --artifact-root "${XDG_STATE_HOME:-$HOME/.local/state}/enginery/artifacts/issue-1"
uv run enginery engine start --database ledger.db --owner operator --request request.json
uv run enginery engine evidence --database ledger.db --owner operator --run-id issue-1 --verify
uv run enginery engine status --database ledger.db --owner operator --stack-id ID
uv run enginery workspace inspect --database ledger.db --owner operator --json
uv run enginery outcome completeness --database ledger.db
uv run enginery workspace release --database ledger.db --owner operator \
  --repository-id ID --run-id ID --dry-run
uv run enginery gate status --gate G4 --database ledger.db --json
uv run enginery gate record-g4-deficiency --help
uv run enginery gate record-g4-deficiency-evidence --help
uv run enginery runner build-image                         # pinned isolated worker image
uv run enginery runner status --image sha256:IMAGE_ID          # engine + pinned image admitted?
uv run enginery capability lock --check
uv run enginery capability inspect skill-a@1 --project-source .@<base-commit-sha> \
  --project-subdir capabilities --json
uv run enginery capability preview skill-a@1 --project-source .@<base-commit-sha> \
  --project-subdir capabilities
uv run enginery capability install skill-a@1 --project-source .@<base-commit-sha> \
  --project-subdir capabilities
uv run enginery capability uninstall
```

See [`docs/operations.md`](docs/operations.md) for the full command
surface and [`docs/adapters.md`](docs/adapters.md) for the adapter/port
contracts.

### Self-development and runtime upgrades

Enginery can develop its own repository only with the isolated runner, which keeps the installed runtime, configuration, ledger and credentials outside the worker. Validation and acceptance commands use the worker image's `enginery-checkout` entry point to run the candidate checkout. To upgrade or roll back the runtime you run Enginery with, use the repository-only launcher `scripts/enginery_runtime.py` (`install WHEEL --yes-sha256 HEX`, `list`, `rollback`). It installs each confirmed wheel into its own environment and switches an external shim. See [Self-development and external runtime management](docs/operations.md#self-development-and-external-runtime-management).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pytest -q
uv run python scripts/verify_project_identity.py
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for package-boundary rules and
[`SECURITY.md`](SECURITY.md) for the vulnerability-reporting process.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
