# Contributing to Enginery

Enginery is pre-`1.0`: releases are published (see the
[README status](README.md#status)), and Python import paths, APIs and schemas can
still change between minor releases (see [`RELEASE_NOTES.md`](RELEASE_NOTES.md)).

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

The source tree is a modular monolith with strict inward dependencies:

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
