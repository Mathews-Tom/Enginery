# Contributing to Enginery

Enginery is in early, pre-`v0.1.0` development. The milestone plan and much
of the architecture are still landing; expect the external contribution
surface to open once Stage 1 (`v0.1.0`) ships.

## Development setup

```bash
uv sync --all-extras --dev
```

## Local gate

Run before every commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
uv run pytest -q
uv run python scripts/verify_project_identity.py
```

## Commit conventions

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
for every commit subject: `<type>[optional scope]: <description>`, imperative
mood, lowercase start, no trailing period.

## Package boundaries

The source tree is a modular monolith with strict inward dependencies
(`03_SYSTEM_DESIGN.md` §8.1):

- `domain` imports nothing else in this repository.
- `application` may import `domain`.
- `engine`, `ledger`, `policy`, `evidence`, and `evaluation` implement
  application services without importing `adapters` or `cli`.
- provider-specific code lives only under `adapters`.
- `cli` invokes application services and owns presentation only.

Verify a layer with:

```bash
uv run python scripts/check_import_boundaries.py <layer>
```
