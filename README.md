# Enginery

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Enginery is an open-source, local-first control plane for agentic
engineering. It coordinates humans, coding agents, and deterministic tools
through governed workflows that turn engineering intent into verified
software outcomes, and improve the system that produces them.

## Status

`v0.3.0`, published on PyPI and GitHub Releases (`v0.1.0`, `v0.2.0`,
and `v0.3.0` are all live). Enginery ships three of its four planned
workflow stages: a coordinator-owned issue-to-merge-ready-pull-request
workflow (Stage 1), a dependency-ordered plan-to-verified-release
workflow with a second coding-agent harness and capability provenance
(Stage 2), and a production-incident-to-hotfix-and-rollback workflow
against a controlled local service (Stage 3) — each proven end to end
against real GitHub/OMP/Claude Code/PyPI destinations, not simulations.
Stage 4 (governed factory self-improvement) is gate-deferred with no
committed date. See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) and
[`CHANGELOG.md`](CHANGELOG.md) for the full compatibility statement.

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
uv run enginery stage1 start --database ledger.db --owner operator --request request.json
uv run enginery stage2 status --database ledger.db --owner operator --stack-id ID
uv run enginery outcome completeness --database ledger.db
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
