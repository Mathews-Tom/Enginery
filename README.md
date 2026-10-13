# Enginery

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Enginery is an open-source, local-first control plane for agentic
engineering. It coordinates humans, coding agents, and deterministic tools
through governed workflows that turn engineering intent into verified
software outcomes, and improve the system that produces them.

## Status

`v0.6.0` is the current published release (PyPI and GitHub Release, 2026-10-13).
`v0.1.0`, `v0.2.0`, `v0.3.0`, `v0.4.0`, and `v0.5.0` (2026-08-11) are also
published on PyPI and GitHub Releases. Enginery ships three of its four planned
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
`enginery gate status --gate G4` to see exactly which entry conditions
are currently met. See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) and
[`CHANGELOG.md`](CHANGELOG.md) for the full compatibility statement.

### Proposed factory direction (not shipped)

Everything above describes shipped behavior. A manual-first software-factory
layer on top of the shipped substrate is **proposed and not shipped**. The
current CLI has no `enginery factory ...` command; the proposed command
family (`init`, `collect`, `run`, `status`, `explain`, `resume`, `pause`,
`drain`, `review`, `watch`, `evidence`) does not exist yet and must not be
scripted against. The proposal keeps the same boundary the shipped substrate
already has: control is manual-first, local-first, and deterministic. Models
never poll, schedule, keep books, or decide whether a check is complete;
routine control uses no model. Harness, model, session, and worker machine are
separate axes and none implies another.

### Licensing and security limits

No upstream skills, prompts, or code from reference projects are vendored;
any pattern learned from them is independently re-implemented under this
repository's Apache-2.0 license. Trusted-local operation is not containment:
a worker process can reach the operator's account, filesystem, network, and
keychain, and Enginery makes no claim of safety against hostile code.
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

## CLI

```bash
uv run enginery --version
uv run enginery doctor
uv run enginery adapter doctor --json
uv run enginery ledger verify --database ledger.db
uv run enginery policy explain request.json
uv run enginery stage1 build-request --output request.json --run-id issue-1 \
  --repository owner/repo --external-reference "owner/repo#1" \
  --base-revision "..." --repository-path /path/to/repo \
  --workspace-path /path/to/workspace --artifact-root /path/to/artifacts
uv run enginery stage1 start --database ledger.db --owner operator --request request.json
uv run enginery stage2 status --database ledger.db --owner operator --stack-id ID
uv run enginery workspace inspect --database ledger.db --owner operator --json
uv run enginery outcome completeness --database ledger.db
uv run enginery workspace release --database ledger.db --owner operator \
  --repository-id ID --run-id ID --dry-run
uv run enginery gate status --gate G4 --database ledger.db --json
uv run enginery gate record-g4-deficiency --help
uv run enginery gate record-g4-deficiency-evidence --help
uv run enginery capability lock --check
```

See [`docs/operations.md`](docs/operations.md) for the full command
surface and [`docs/adapters.md`](docs/adapters.md) for the adapter/port
contracts.

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
