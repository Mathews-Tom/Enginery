# Release Evidence

Post-publication evidence for every Enginery release-preparation pass.
Every result below was directly executed and observed in its own
releasing session; none is inferred or copied from a prior report.
Consolidated into one file, newest first, replacing the previous
`release-evidence-v0.1.0.md`/`release-evidence-v0.2.0.md`/`release-evidence-v0.3.0.md`
per-version files — each version's own evidence is fully preserved
below, not summarized away.

## `v0.3.0`

Post-publication evidence for the M13b release-preparation pass.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#133](https://github.com/Mathews-Tom/Enginery/pull/133) | `test(release): extend full_system_gate.py with a restart-capable Stage 3 leg` | `7bc634a0934def1a202d298fe7a0cc3601dc5bbd` |
| [#134](https://github.com/Mathews-Tom/Enginery/pull/134) | `build(release): prepare v0.3.0 version, changelog, and artifacts` | `04a83343cacc9378e16a8face12ae148756f5a46` |
| [#135](https://github.com/Mathews-Tom/Enginery/pull/135) | `docs(release): finalize v0.3.0 compatibility statement and release notes` | `2d275843d84ab5a59a170fe9f25099ff77d8da05` |

PR #133 is a pre-release tooling PR (not one of the milestone's two
named release-preparation PRs), landed after the M13b reassessment
found `scripts/full_system_gate.py` did not support the milestone's
own required `--stages 1,2,3` invocation — the same release-tooling-gap
pattern the M12b reassessment already found and resolved once before.
PRs #134 and #135 are M13b's own `m13b/release-01`/`m13b/release-02`
stack.

`origin/main` at `2d275843d84ab5a59a170fe9f25099ff77d8da05` passed CI
(`macos-latest` and `ubuntu-latest`, both `success`) as the exact
commit under verification, re-triggered after the last merge (not
trusted from the pre-merge PR checks).

### Tag

Annotated tag `v0.3.0` (object `412a10b1be0feb4dfe9a73260b57b7103321e8fd`)
targets commit `2d275843d84ab5a59a170fe9f25099ff77d8da05` -- the same
commit CI verified above. Confirmed via the GitHub git-data API
(`GET repos/Mathews-Tom/Enginery/git/ref/tags/v0.3.0` ->
`git/tags/{sha}` -> `object.sha` == the commit above).

### Build artifacts

Built once from the tagged commit with `uv run python scripts/release_gate.py --version 0.3.0`:

| Artifact | `sha256` |
|---|---|
| `enginery-0.3.0-py3-none-any.whl` | `b6668cfe97ff871e7824b1eb66c33283b40ea27596a986da15ba58902e7bb3b8` |
| `enginery-0.3.0.tar.gz` | `ebca4cb926930b8a872ed8dd48fd096b7cc913dea28cfc3f1f57d6f12985ab16` |

`uvx twine check dist/*` passed for both artifacts. `scripts/release_gate.py`'s
clean-install smoke (macOS, isolated venv) passed.

### Clean-install smoke

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

### Publication

- **PyPI:** <https://pypi.org/project/enginery/0.3.0/>. `uv publish` to
  `https://upload.pypi.org/legacy/`. Confirmed via the public JSON API
  (`GET https://pypi.org/pypi/enginery/json`): reports version `0.3.0`
  with both artifacts' `sha256` digests matching the table above
  exactly.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.3.0>.
  `gh release create v0.3.0 --verify-tag --notes-file RELEASE_NOTES.md`
  with both artifacts attached. Not a draft. Both attached assets'
  reported `digest` fields match the table above exactly.

### Post-publication verification

`uv run python scripts/verify_published_release.py --confirm-published
--repository Mathews-Tom/Enginery --tag-name v0.3.0 --target-commitish
2d275843d84ab5a59a170fe9f25099ff77d8da05 --project-name enginery
--version 0.3.0 --artifact
enginery-0.3.0-py3-none-any.whl=b6668cfe97... --artifact
enginery-0.3.0.tar.gz=ebca4cb926...` -- `PASS`. Independently confirms:
the `v0.3.0` tag resolves to the exact released commit; the GitHub
Release is not a draft and both named assets' digests match; PyPI
reports `enginery 0.3.0` with both artifacts' `sha256` digests
matching. This script needed no code changes for `v0.3.0` -- it is
fully version/project-generic via its CLI arguments, unchanged since
`v0.1.0`.

### Human publication approval

Recorded via the `ask` tool in the releasing session on 2026-07-21:
"Approve: publish v0.3.0 to PyPI and GitHub Releases" -- selected after
the tag was pushed and all pre-publication verification (build, hashes,
clean-install smoke on macOS and a real Ubuntu 24.04 container,
cumulative Stage 1+2+3 gate) had already passed.

### Final verdict

**GO.** `v0.3.0` is tagged from the CI-verified `origin/main` commit;
PyPI and the GitHub Release both contain the intended artifacts with
matching hashes; a clean install from the real PyPI index passes the
CLI and cumulative Stage 1+2+3 gate smoke on both macOS and a real
Ubuntu container; all release-prep branches were merged and are
cleaned up; `RELEASE_NOTES.md` correctly scopes this release to Stage 3
(incident to hotfix and rollback) layered on `v0.1.0`/`v0.2.0`, and
discloses that Stage 4 remains gate-deferred with no committed date.

## `v0.2.0`

Post-publication evidence for the M12b release-preparation pass.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#123](https://github.com/Mathews-Tom/Enginery/pull/123) | `build(release): support cumulative Stage 1+2 gate in full_system_gate.py` | `bee136b185c4dca87d5a23e7b2cb72a0b87d4f33` |
| [#124](https://github.com/Mathews-Tom/Enginery/pull/124) | `build(release): prepare v0.2.0 version, changelog, and dependency manifest` | `8bc1ee97d8a93bcb1f198cfad1c926cefe45ad9a` |
| [#125](https://github.com/Mathews-Tom/Enginery/pull/125) | `docs(release): finalize v0.2.0 compatibility and release notes` | `f989e0d74c39dea48bed952bff519f02f5829810` |

PR #123 is a pre-release tooling PR (not one of the milestone's two
named release-preparation PRs), landed after the M12b reassessment
found `scripts/full_system_gate.py` did not support the milestone's own
required `--stages 1,2` invocation. PRs #124 and #125 are M12b's own
`m12b/release-01`/`m12b/release-02` stack.

`origin/main` at `f989e0d74c39dea48bed952bff519f02f5829810` passed CI
(`macos-latest` and `ubuntu-latest`, both `success`) as the exact commit
under verification.

### Tag

Annotated tag `v0.2.0` (object `41b436baca99d7bd78bff6c9f4b343b362f10084`)
targets commit `f989e0d74c39dea48bed952bff519f02f5829810` -- the same
commit CI verified above. Confirmed via the GitHub git-data API
(`GET repos/Mathews-Tom/Enginery/git/ref/tags/v0.2.0` ->
`git/tags/{sha}` -> `object.sha` == the commit above).

### Build artifacts

Built once from the tagged commit with `uv run python scripts/release_gate.py --version 0.2.0`:

| Artifact | `sha256` |
|---|---|
| `enginery-0.2.0-py3-none-any.whl` | `e5c79c3a9ffbb5e35bc53e9401974851fc0b30da1701e7881013da018b728aa1` |
| `enginery-0.2.0.tar.gz` | `6120fa1f1cacdec382753e27e40f7e16d84c732e85cb01b66dae5d4055ae7016` |

`uvx twine check dist/*` passed. `scripts/release_gate.py`'s
clean-install smoke (macOS, isolated venv) passed.

### Clean-install smoke

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

### Publication

- **PyPI:** <https://pypi.org/project/enginery/0.2.0/>. `uv publish` to
  `https://upload.pypi.org/legacy/`. Confirmed via the public JSON API
  (`GET https://pypi.org/pypi/enginery/0.2.0/json`): reports version
  `0.2.0` with both artifacts' `sha256` digests matching the table above
  exactly.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.2.0>.
  `gh release create v0.2.0 --verify-tag --notes-file RELEASE_NOTES.md`
  with both artifacts attached. Not a draft. Both attached assets'
  reported `digest` fields match the table above exactly.

### Post-publication verification

`uv run python scripts/verify_published_release.py --confirm-published
--repository Mathews-Tom/Enginery --tag-name v0.2.0 --target-commitish
f989e0d74c39dea48bed952bff519f02f5829810 --project-name enginery
--version 0.2.0 --artifact
enginery-0.2.0-py3-none-any.whl=e5c79c3a9f... --artifact
enginery-0.2.0.tar.gz=6120fa1f1c...` -- `PASS`. Independently confirms:
the `v0.2.0` tag resolves to the exact released commit; the GitHub
Release is not a draft and both named assets' digests match; PyPI
reports `enginery 0.2.0` with both artifacts' `sha256` digests
matching. This script needed no code changes for `v0.2.0` -- it is
fully version/project-generic via its CLI arguments, unchanged since
`v0.1.0`.

### Human publication approval

Recorded via the `ask` tool in the releasing session on 2026-07-21:
"Approve: publish v0.2.0 now" -- selected after the tag was pushed and
all pre-publication verification (build, hashes, clean-install smoke on
macOS and Ubuntu, cumulative Stage 1+2 gate) had already passed.

### Final verdict

**GO.** `v0.2.0` is tagged from the CI-verified `origin/main` commit;
PyPI and the GitHub Release both contain the intended artifacts with
matching hashes; a clean install from the real PyPI index passes the
CLI and cumulative Stage 1+2 gate smoke; all release-prep branches were
merged and are cleaned up; `RELEASE_NOTES.md` correctly scopes this
release to Stage 1 (carried forward) plus Stage 2, second-harness
neutrality, and capability provenance, and discloses that Stage 3 ships
in `v0.3.0` and Stage 4 remains gate-deferred with no committed date.

## `v0.1.0`

Post-publication evidence for the M17 release-preparation pass.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#120](https://github.com/Mathews-Tom/Enginery/pull/120) | `build(release): prepare v0.1.0 version, changelog, and release gate` | `fbf0213077f3bc968e375c50c475db74eb33f626` |
| [#121](https://github.com/Mathews-Tom/Enginery/pull/121) | `docs(release): finalize compatibility, migration, and release notes` | `5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d` |

`origin/main` at `5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d` passed CI
(`macos-latest` and `ubuntu-latest`, both `success`) as the exact commit
under verification.

### Tag

Annotated tag `v0.1.0` (object `97c53664a14763dc2ea189813f4c945c72d14f3b`)
targets commit `5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d` -- the same
commit CI verified above. Confirmed both locally (`git tag -v v0.1.0`)
and via the GitHub git-data API
(`GET repos/Mathews-Tom/Enginery/git/ref/tags/v0.1.0` ->
`git/tags/{sha}` -> `object.sha` == the commit above).

### Build artifacts

Built once from the tagged commit with `uv run python scripts/release_gate.py --version 0.1.0`:

| Artifact | `sha256` |
|---|---|
| `enginery-0.1.0-py3-none-any.whl` | `ff9705d1591f8e0b5ba6e896459067752b3efd157f9c5b78054cd1b85b1a3505` |
| `enginery-0.1.0.tar.gz` | `9ee426f3c9d2e2fdb2fe22161e88007f482c37cc2492f46ded4280826c97b7d9` |

`uvx twine check dist/*` passed. `scripts/release_gate.py`'s
clean-install smoke (macOS, isolated venv) passed.

### Clean-install smoke

| Platform | `--version` | `doctor` | Stage 1 local workflow (`full_system_gate.py --stages 1 --restart-between-stages`) |
|---|---|---|---|
| macOS (Apple M1 Pro, Python 3.12.8) | `enginery 0.1.0` | `[ok]` | `PASS ... evidence_digest=sha256:c322447265266db65ab1e82c3db54a3335c889e83a1ad032fcf8c57941124f4e` |
| Ubuntu 24.04 (Docker, Python 3.12.3) | `enginery 0.1.0` | `[ok]` | `PASS ... evidence_digest=sha256:b40f96fe38460efd7af56e98e93a962e51f3f08cddef9f16b267fa45fae9fc30` |
| Real PyPI install (macOS host, fresh venv) | `enginery 0.1.0` | `[ok]` | `PASS ... evidence_digest=sha256:5974f036a24a9193dcfbc24082245b6e9729efefd95905a2e783e3b1a9a3cf79` |

Each row is a separate isolated virtual environment with the wheel
installed clean (no editable install, no prior `enginery` state).

### Publication

- **PyPI:** <https://pypi.org/project/enginery/0.1.0/>. `uv publish` to
  `https://upload.pypi.org/legacy/`. Confirmed via the public JSON API
  (`GET https://pypi.org/pypi/enginery/0.1.0/json`): reports version
  `0.1.0` with both artifacts' `sha256` digests matching the table above
  exactly.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.1.0>.
  `gh release create v0.1.0 --verify-tag --notes-file RELEASE_NOTES.md`
  with both artifacts attached. Not a draft. Both attached assets'
  reported `digest` fields match the table above exactly.

### Post-publication verification

`uv run python scripts/verify_published_release.py --confirm-published
--repository Mathews-Tom/Enginery --tag-name v0.1.0 --target-commitish
5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d --project-name enginery
--version 0.1.0 --artifact
enginery-0.1.0-py3-none-any.whl=ff9705d1... --artifact
enginery-0.1.0.tar.gz=9ee426f3...` -- `PASS`. Independently confirms:
the `v0.1.0` tag resolves to the exact released commit; the GitHub
Release is not a draft and both named assets' digests match; PyPI
reports `enginery 0.1.0` with both artifacts' `sha256` digests
matching.

While building this script's fixed verification path, `gh release view
--json targetCommitish` was found to report the repository's default
branch name (`main`) rather than the tagged commit's `sha` for a
release created from an *already-existing* tag (this repository's own
convention: the tag is pushed before the release is created). The
script was corrected to resolve the tag ref itself
(`git/ref/tags/{tag}` -> dereferenced through `git/tags/{sha}` for an
annotated tag) rather than trusting that field, and the corrected
script produced the `PASS` result recorded above. The originally
published PyPI and GitHub Release artifacts were unaffected -- only the
read-only verification script had the defect, not the release itself.

### Final verdict

**GO.** `v0.1.0` is tagged from the CI-verified `origin/main` commit;
PyPI and the GitHub Release both contain the intended artifacts with
matching hashes; a clean install from the real PyPI index passes the
CLI and Stage 1 workflow smoke; all release-prep branches were merged
and are cleaned up; `RELEASE_NOTES.md` correctly scopes this release to
Stage 1 only and discloses the Stage 4 gate deferral with no committed
date.
