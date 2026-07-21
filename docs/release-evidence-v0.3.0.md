# `v0.3.0` Release Evidence

Post-publication evidence for the M13b release-preparation pass. Every
result below was directly executed and observed in the releasing
session; none is inferred or copied from a prior report.

## Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#133](https://github.com/Mathews-Tom/Enginery/pull/133) | `test(release): extend full_system_gate.py with a restart-capable Stage 3 leg` | `7bc634a0934def1a202d298fe7a0cc3601dc5bbd` |
| [#134](https://github.com/Mathews-Tom/Enginery/pull/134) | `build(release): prepare v0.3.0 version, changelog, and artifacts` | `04a83343cacc9378e16a8face12ae148756f5a46` |
| [#135](https://github.com/Mathews-Tom/Enginery/pull/135) | `docs(release): finalize v0.3.0 compatibility statement and release notes` | `2d275843d84ab5a59a170fe9f25099ff77d8da05` |

PR #133 is a pre-release tooling PR (not one of the milestone's two named
release-preparation PRs), landed after the M13b reassessment found
`scripts/full_system_gate.py` did not support the milestone's own
required `--stages 1,2,3` invocation — the same release-tooling-gap
pattern the M12b reassessment already found and resolved once before.
PRs #134 and #135 are M13b's own `m13b/release-01`/`m13b/release-02`
stack.

`origin/main` at `2d275843d84ab5a59a170fe9f25099ff77d8da05` passed CI
(`macos-latest` and `ubuntu-latest`, both `success`) as the exact commit
under verification, re-triggered after the last merge (not trusted from
the pre-merge PR checks).

## Tag

Annotated tag `v0.3.0` (object `412a10b1be0feb4dfe9a73260b57b7103321e8fd`)
targets commit `2d275843d84ab5a59a170fe9f25099ff77d8da05` -- the same
commit CI verified above. Confirmed via the GitHub git-data API
(`GET repos/Mathews-Tom/Enginery/git/ref/tags/v0.3.0` ->
`git/tags/{sha}` -> `object.sha` == the commit above).

## Build artifacts

Built once from the tagged commit with `uv run python scripts/release_gate.py --version 0.3.0`:

| Artifact | `sha256` |
|---|---|
| `enginery-0.3.0-py3-none-any.whl` | `b6668cfe97ff871e7824b1eb66c33283b40ea27596a986da15ba58902e7bb3b8` |
| `enginery-0.3.0.tar.gz` | `ebca4cb926930b8a872ed8dd48fd096b7cc913dea28cfc3f1f57d6f12985ab16` |

`uvx twine check dist/*` passed for both artifacts. `scripts/release_gate.py`'s
clean-install smoke (macOS, isolated venv) passed.

## Clean-install smoke

| Platform | Artifact | `--version` | `doctor` |
|---|---|---|---|
| macOS (Apple M1 Pro, Python 3.12.8) | wheel + sdist, local `dist/` | `enginery 0.3.0` | `[ok]` |
| Ubuntu 24.04 (real Docker container, not the CI lint job) | wheel, local `dist/` | `enginery 0.3.0` | `[ok]` |
| Ubuntu 24.04 (real Docker container) | sdist, local `dist/` | `enginery 0.3.0` | `[ok]` |
| macOS (fresh venv, real PyPI index) | `enginery==0.3.0` | `enginery 0.3.0` | `[ok]` |
| Ubuntu 24.04 (real Docker container, real PyPI index) | `enginery==0.3.0` | `enginery 0.3.0` | `[ok]` |

Each row is a separate isolated virtual environment with no editable
install and no prior `enginery` state. Unlike `v0.1.0`/`v0.2.0`, the
Ubuntu rows are genuinely executed against a real `ubuntu:24.04`
container (not merely inferred from CI's `ubuntu-latest` lint/test job,
which never runs `scripts/release_gate.py`'s clean-install path).

`uv run python scripts/full_system_gate.py --stages 1,2,3
--restart-between-stages` passed against the exact tagged commit
(`stage1_runs=2 stage2_evidence=yes stage3_evidence=yes
evidence_digest=sha256:4ef8b93e471f101a710f4a1efba333e0ec3dc2d36f9e1f8268275d19df60ff6f`) --
the gate's own Stage 3 leg (new in this release, added by PR #133)
proves Stage 3's ingest -> hotfix -> deploy -> observe -> roll back ->
restore sequence locally with a real subprocess-managed local HTTP
service, no external credential or network call.

## Publication

- **PyPI:** <https://pypi.org/project/enginery/0.3.0/>. `uv publish` to
  `https://upload.pypi.org/legacy/`. Confirmed via the public JSON API
  (`GET https://pypi.org/pypi/enginery/json`): reports version `0.3.0`
  with both artifacts' `sha256` digests matching the table above
  exactly.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.3.0>.
  `gh release create v0.3.0 --verify-tag --notes-file RELEASE_NOTES.md`
  with both artifacts attached. Not a draft. Both attached assets'
  reported `digest` fields match the table above exactly.

## Post-publication verification

`uv run python scripts/verify_published_release.py --confirm-published
--repository Mathews-Tom/Enginery --tag-name v0.3.0 --target-commitish
2d275843d84ab5a59a170fe9f25099ff77d8da05 --project-name enginery
--version 0.3.0 --artifact
enginery-0.3.0-py3-none-any.whl=b6668cfe97... --artifact
enginery-0.3.0.tar.gz=ebca4cb926...` -- `PASS`. Independently confirms:
the `v0.3.0` tag resolves to the exact released commit; the GitHub
Release is not a draft and both named assets' digests match; PyPI
reports `enginery 0.3.0` with both artifacts' `sha256` digests matching.
This script needed no code changes for `v0.3.0` -- it is fully
version/project-generic via its CLI arguments, unchanged since
`v0.1.0`.

## Human publication approval

Recorded via the `ask` tool in the releasing session on 2026-07-21:
"Approve: publish v0.3.0 to PyPI and GitHub Releases" -- selected after
the tag was pushed and all pre-publication verification (build, hashes,
clean-install smoke on macOS and a real Ubuntu 24.04 container,
cumulative Stage 1+2+3 gate) had already passed.

## Final verdict

**GO.** `v0.3.0` is tagged from the CI-verified `origin/main` commit;
PyPI and the GitHub Release both contain the intended artifacts with
matching hashes; a clean install from the real PyPI index passes the
CLI and cumulative Stage 1+2+3 gate smoke on both macOS and a real
Ubuntu container; all release-prep branches were merged and are
cleaned up; `RELEASE_NOTES.md` correctly scopes this release to Stage 3
(incident to hotfix and rollback) layered on `v0.1.0`/`v0.2.0`, and
discloses that Stage 4 remains gate-deferred with no committed date.
