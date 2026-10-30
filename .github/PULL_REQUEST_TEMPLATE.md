## Summary



## Risk

<!-- Low / Medium / High — set by the change, not the author — plus one line on why. Low: docs, comments, wording. Medium: ordinary features, fixes, refactors. High: release/publish, CI workflows, credentials, anything that pushes to GitHub or (Test)PyPI. -->

## Testing

<!-- What you ran and what it returned. Low-risk changes: CI green is enough. -->

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy --strict src`
- [ ] `uv run pytest -q`
- [ ] `uv run python scripts/verify_project_identity.py`

## How to revert

<!-- Usually "revert this PR". Name anything a revert does not undo, e.g. a published release, pushed tags, or state written outside the repo. -->

## Notes
