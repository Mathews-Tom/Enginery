# Enginery

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Enginery is an open-source, local-first control plane for agentic
engineering. It coordinates humans, coding agents, and deterministic tools
through governed workflows that turn engineering intent into verified
software outcomes, and improve the system that produces them.

## Status

`v0.1.0` (Stage 1 only). Enginery ships a coordinator-owned, durable
issue-to-merge-ready-pull-request workflow, proven end to end against a
real GitHub repository and a real coding-agent harness, plus a versioned
raw outcome-observation schema. Stage 2 (plan to verified release) and
Stage 3 (incident to hotfix) ship in later release trains (`v0.2.0`,
`v0.3.0`); Stage 4 (governed factory self-improvement) is gate-deferred
with no committed date. See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) and
[`CHANGELOG.md`](CHANGELOG.md) for the full compatibility statement.

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
```

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
