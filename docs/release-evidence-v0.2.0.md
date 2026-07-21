# `v0.2.0` Release Evidence

Post-publication evidence for the M12b release-preparation pass. Every
result below was directly executed and observed in the releasing
session; none is inferred or copied from a prior report.

## Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#123](https://github.com/Mathews-Tom/Enginery/pull/123) | `build(release): support cumulative Stage 1+2 gate in full_system_gate.py` | `bee136b185c4dca87d5a23e7b2cb72a0b87d4f33` |
| [#124](https://github.com/Mathews-Tom/Enginery/pull/124) | `build(release): prepare v0.2.0 version, changelog, and dependency manifest` | `8bc1ee97d8a93bcb1f198cfad1c926cefe45ad9a` |
| [#125](https://github.com/Mathews-Tom/Enginery/pull/125) | `docs(release): finalize v0.2.0 compatibility and release notes` | `f989e0d74c39dea48bed952bff519f02f5829810` |

PR #123 is a pre-release tooling PR (not one of the milestone's two named
release-preparation PRs), landed after the M12b reassessment found
`scripts/full_system_gate.py` did not support the milestone's own
required `--stages 1,2` invocation. PRs #124 and #125 are M12b's own
`m12b/release-01`/`m12b/release-02` stack.

`origin/main` at `f989e0d74c39dea48bed952bff519f02f5829810` passed CI
(`macos-latest` and `ubuntu-latest`, both `success`) as the exact commit
under verification.

## Tag

Annotated tag `v0.2.0` (object `41b436baca99d7bd78bff6c9f4b343b362f10084`)
targets commit `f989e0d74c39dea48bed952bff519f02f5829810` -- the same
commit CI verified above. Confirmed via the GitHub git-data API
(`GET repos/Mathews-Tom/Enginery/git/ref/tags/v0.2.0` ->
`git/tags/{sha}` -> `object.sha` == the commit above).

## Build artifacts

Built once from the tagged commit with `uv run python scripts/release_gate.py --version 0.2.0`:

| Artifact | `sha256` |
|---|---|
| `enginery-0.2.0-py3-none-any.whl` | `e5c79c3a9ffbb5e35bc53e9401974851fc0b30da1701e7881013da018b728aa1` |
| `enginery-0.2.0.tar.gz` | `6120fa1f1cacdec382753e27e40f7e16d84c732e85cb01b66dae5d4055ae7016` |

`uvx twine check dist/*` passed. `scripts/release_gate.py`'s
clean-install smoke (macOS, isolated venv) passed.

## Clean-install smoke

| Platform | `--version` | `doctor` | Cumulative Stage 1+2 gate (`full_system_gate.py --stages 1,2 --restart-between-stages`) |
|---|---|---|---|
| macOS (Apple M1 Pro, Python 3.12.8) | `enginery 0.2.0` | `[ok]` | `PASS ... evidence_digest=sha256:94f58d44bbd1b13da1518f09bd9f4fc564b880f555d1bb7f85a2acc66288dbdb` |
| Ubuntu (Docker `python:3.12-slim`, Python 3.12.13) | `enginery 0.2.0` | `[ok]` | `PASS ... evidence_digest=sha256:42cbb3f7e709782b8bbc40ecb0291836e71375dee69d86217831b1275856c92c` |
| Real PyPI install (macOS host, fresh venv) | `enginery 0.2.0` | `[ok]` | `PASS ... evidence_digest=sha256:3f2f114ffd1a1bd7c601639f5eb06dd2a8684d5a93244af093f2625c147ad998` |

Each row is a separate isolated virtual environment with the wheel
installed clean (no editable install, no prior `enginery` state). The
gate's own Stage 2 leg (new in this release, added by PR #123) proves
Stage 2's merge -> prepare -> build -> publish -> verify sequence
locally, with no live GitHub/PyPI network or credential access.

## Publication

- **PyPI:** <https://pypi.org/project/enginery/0.2.0/>. `uv publish` to
  `https://upload.pypi.org/legacy/`. Confirmed via the public JSON API
  (`GET https://pypi.org/pypi/enginery/0.2.0/json`): reports version
  `0.2.0` with both artifacts' `sha256` digests matching the table above
  exactly.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.2.0>.
  `gh release create v0.2.0 --verify-tag --notes-file RELEASE_NOTES.md`
  with both artifacts attached. Not a draft. Both attached assets'
  reported `digest` fields match the table above exactly.

## Post-publication verification

`uv run python scripts/verify_published_release.py --confirm-published
--repository Mathews-Tom/Enginery --tag-name v0.2.0 --target-commitish
f989e0d74c39dea48bed952bff519f02f5829810 --project-name enginery
--version 0.2.0 --artifact
enginery-0.2.0-py3-none-any.whl=e5c79c3a9f... --artifact
enginery-0.2.0.tar.gz=6120fa1f1c...` -- `PASS`. Independently confirms:
the `v0.2.0` tag resolves to the exact released commit; the GitHub
Release is not a draft and both named assets' digests match; PyPI
reports `enginery 0.2.0` with both artifacts' `sha256` digests matching.
This script needed no code changes for `v0.2.0` -- it is fully
version/project-generic via its CLI arguments, unchanged since `v0.1.0`.

## Human publication approval

Recorded via the `ask` tool in the releasing session on 2026-07-21:
"Approve: publish v0.2.0 now" -- selected after the tag was pushed and
all pre-publication verification (build, hashes, clean-install smoke on
macOS and Ubuntu, cumulative Stage 1+2 gate) had already passed.

## Final verdict

**GO.** `v0.2.0` is tagged from the CI-verified `origin/main` commit;
PyPI and the GitHub Release both contain the intended artifacts with
matching hashes; a clean install from the real PyPI index passes the
CLI and cumulative Stage 1+2 gate smoke; all release-prep branches were
merged and are cleaned up; `RELEASE_NOTES.md` correctly scopes this
release to Stage 1 (carried forward) plus Stage 2, second-harness
neutrality, and capability provenance, and discloses that Stage 3 ships
in `v0.3.0` and Stage 4 remains gate-deferred with no committed date.
