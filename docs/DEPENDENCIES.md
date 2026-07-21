# Dependency Manifest

Enginery's runtime dependency footprint is intentionally minimal. This
is a plain-language manifest generated from the resolved `uv.lock`;
full pinned versions and per-platform `sha256` wheel/sdist hashes are
recorded in [`uv.lock`](../uv.lock), which `uv build`/`uv sync` verify
on every install. This document is the human-readable summary required
as release evidence for every version (`DEVELOPMENT_PLAN.md`'s
"SBOM or dependency manifest" deliverable); it is not itself a build
input.

Consolidated into one file, replacing the previous
`dependencies-v0.1.0.md`/`dependencies-v0.2.0.md`/`dependencies-v0.3.0.md`
per-version files: the dependency graph has not changed across any
release to date, so per-version duplication added no information. The
[version history](#version-history) table below records exactly what
changed, or didn't, at each release; add a new row there the next time
a release actually changes this graph.

## Current dependency graph (`v0.3.0`)

### Base install (`pip install enginery`)

```text
enginery
└── cryptography v49.0.0
    └── cffi v2.1.0
        └── pycparser v3.0
```

### With the `armory` extra (`pip install enginery[armory]`)

```text
enginery
├── cryptography v49.0.0
│   └── cffi v2.1.0
│       └── pycparser v3.0
└── pyyaml v6.0.3
```

`armory` is optional: it is required only for the disabled-by-default
Armory capability-registry adapter (`enginery.adapters.armory`). The
base install has no YAML dependency.

## License summary

Generated with `pip-licenses --format=markdown --with-urls` against a
clean install (`uv pip install enginery-0.3.0-py3-none-any.whl[armory]`)
in an isolated virtual environment:

| Name         | Version | License                    | URL                                                 |
|--------------|---------|----------------------------|------------------------------------------------------|
| enginery     | 0.3.0   | Apache-2.0                 | https://github.com/Mathews-Tom/Enginery              |
| cryptography | 49.0.0  | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography                 |
| cffi         | 2.1.0   | MIT-0                      | https://cffi.readthedocs.io/en/latest/whatsnew.html  |
| pycparser    | 3.0     | BSD-3-Clause               | https://github.com/eliben/pycparser                  |
| PyYAML       | 6.0.3   | MIT License                | https://pyyaml.org/                                  |

Every listed license is OSI-approved and compatible with Enginery's own
Apache-2.0 license. No dependency carries a copyleft (GPL-family)
license. This table has been identical, aside from the `enginery`
version number itself, since `v0.1.0`.

## Build-time and development-only dependencies

`hatchling>=1.25` (the `[build-system]` backend) and the `dev`
dependency group (`mypy`, `pytest`, `ruff`, `types-PyYAML`) are never
installed by an end-user `pip install enginery` — they exist only in
this repository's own `uv sync --all-extras --dev` development
environment and are not part of the published distribution's
dependency graph.

## Version history

| Version | Dependency graph change | Notes |
|---|---|---|
| `v0.1.0` | Baseline: `cryptography`, optional `pyyaml` (via `armory` extra) | First published graph |
| `v0.2.0` | None | Capability locking/provenance, the Claude Code adapter, plan ingestion, and Stage 2 publication all build on the existing `cryptography`/`pyyaml` footprint |
| `v0.3.0` | None | Stage 3's incident, hotfix, and controlled-local-service deployment code is stdlib-only (`http.server`, `subprocess`, `socket`, `urllib.request`, `git` as an external binary) |
