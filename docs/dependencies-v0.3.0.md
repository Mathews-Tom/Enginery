# Dependency Manifest — `v0.3.0`

Enginery's runtime dependency footprint is intentionally minimal. This is
a plain-language manifest generated from the resolved `uv.lock` for the
`v0.3.0` release; full pinned versions and per-platform `sha256` wheel/sdist
hashes are recorded in [`uv.lock`](../uv.lock), which `uv build`/`uv sync`
verify on every install. This document is the human-readable summary
required for release evidence; it is not itself a build input.

`v0.3.0` adds no new runtime dependency over `v0.2.0`: Stage 3's incident,
hotfix, and controlled-local-service deployment code is stdlib-only
(`http.server`, `subprocess`, `socket`, `urllib.request`, `git` as an
external binary) and builds on the same `cryptography`/optional `pyyaml`
footprint already shipped in `v0.1.0` and `v0.2.0`.

## Base install (`pip install enginery`)

```text
enginery v0.3.0
└── cryptography v49.0.0
    └── cffi v2.1.0
        └── pycparser v3.0
```

## With the `armory` extra (`pip install enginery[armory]`)

```text
enginery v0.3.0
├── cryptography v49.0.0
│   └── cffi v2.1.0
│       └── pycparser v3.0
└── pyyaml v6.0.3
```

`armory` is optional: it is required only for the disabled-by-default
Armory capability-registry adapter (`enginery.adapters.armory`). The base
install has no YAML dependency.

## License summary

Generated with `pip-licenses --format=markdown --with-urls` against a
clean `v0.3.0` install (`uv pip install enginery-0.3.0-py3-none-any.whl[armory]`)
in an isolated virtual environment:

| Name         | Version | License                    | URL                                                 |
|--------------|---------|----------------------------|------------------------------------------------------|
| enginery     | 0.3.0   | Apache-2.0                 | https://github.com/Mathews-Tom/Enginery              |
| cryptography | 49.0.0  | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography                 |
| cffi         | 2.1.0   | MIT-0                      | https://cffi.readthedocs.io/en/latest/whatsnew.html  |
| pycparser    | 3.0     | BSD-3-Clause               | https://github.com/eliben/pycparser                  |
| PyYAML       | 6.0.3   | MIT License                | https://pyyaml.org/                                  |

Every listed license is OSI-approved and compatible with Enginery's own
Apache-2.0 license. No dependency carries a copyleft (GPL-family) license.

## Build-time and development-only dependencies

`hatchling>=1.25` (the `[build-system]` backend) and the `dev` dependency
group (`mypy`, `pytest`, `ruff`, `types-PyYAML`) are never installed by an
end-user `pip install enginery` — they exist only in this repository's own
`uv sync --all-extras --dev` development environment and are not part of
the published distribution's dependency graph.
