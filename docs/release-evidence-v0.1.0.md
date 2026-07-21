# `v0.1.0` Release Evidence

Post-publication evidence for the M17 release-preparation pass. Every
result below was directly executed and observed in the releasing
session; none is inferred or copied from a prior report.

## Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#120](https://github.com/Mathews-Tom/Enginery/pull/120) | `build(release): prepare v0.1.0 version, changelog, and release gate` | `fbf0213077f3bc968e375c50c475db74eb33f626` |
| [#121](https://github.com/Mathews-Tom/Enginery/pull/121) | `docs(release): finalize compatibility, migration, and release notes` | `5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d` |

`origin/main` at `5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d` passed CI
(`macos-latest` and `ubuntu-latest`, both `success`) as the exact commit
under verification.

## Tag

Annotated tag `v0.1.0` (object `97c53664a14763dc2ea189813f4c945c72d14f3b`)
targets commit `5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d` -- the same
commit CI verified above. Confirmed both locally (`git tag -v v0.1.0`)
and via the GitHub git-data API
(`GET repos/Mathews-Tom/Enginery/git/ref/tags/v0.1.0` ->
`git/tags/{sha}` -> `object.sha` == the commit above).

## Build artifacts

Built once from the tagged commit with `uv run python scripts/release_gate.py --version 0.1.0`:

| Artifact | `sha256` |
|---|---|
| `enginery-0.1.0-py3-none-any.whl` | `ff9705d1591f8e0b5ba6e896459067752b3efd157f9c5b78054cd1b85b1a3505` |
| `enginery-0.1.0.tar.gz` | `9ee426f3c9d2e2fdb2fe22161e88007f482c37cc2492f46ded4280826c97b7d9` |

`uvx twine check dist/*` passed. `scripts/release_gate.py`'s
clean-install smoke (macOS, isolated venv) passed.

## Clean-install smoke

| Platform | `--version` | `doctor` | Stage 1 local workflow (`full_system_gate.py --stages 1 --restart-between-stages`) |
|---|---|---|---|
| macOS (Apple M1 Pro, Python 3.12.8) | `enginery 0.1.0` | `[ok]` | `PASS ... evidence_digest=sha256:c322447265266db65ab1e82c3db54a3335c889e83a1ad032fcf8c57941124f4e` |
| Ubuntu 24.04 (Docker, Python 3.12.3) | `enginery 0.1.0` | `[ok]` | `PASS ... evidence_digest=sha256:b40f96fe38460efd7af56e98e93a962e51f3f08cddef9f16b267fa45fae9fc30` |
| Real PyPI install (macOS host, fresh venv) | `enginery 0.1.0` | `[ok]` | `PASS ... evidence_digest=sha256:5974f036a24a9193dcfbc24082245b6e9729efefd95905a2e783e3b1a9a3cf79` |

Each row is a separate isolated virtual environment with the wheel
installed clean (no editable install, no prior `enginery` state).

## Publication

- **PyPI:** <https://pypi.org/project/enginery/0.1.0/>. `uv publish` to
  `https://upload.pypi.org/legacy/`. Confirmed via the public JSON API
  (`GET https://pypi.org/pypi/enginery/0.1.0/json`): reports version
  `0.1.0` with both artifacts' `sha256` digests matching the table above
  exactly.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.1.0>.
  `gh release create v0.1.0 --verify-tag --notes-file RELEASE_NOTES.md`
  with both artifacts attached. Not a draft. Both attached assets'
  reported `digest` fields match the table above exactly.

## Post-publication verification

`uv run python scripts/verify_published_release.py --confirm-published
--repository Mathews-Tom/Enginery --tag-name v0.1.0 --target-commitish
5c7428bc0cc46bd60f6d34cdf1eb0195f4f9c47d --project-name enginery
--version 0.1.0 --artifact
enginery-0.1.0-py3-none-any.whl=ff9705d1... --artifact
enginery-0.1.0.tar.gz=9ee426f3...` -- `PASS`. Independently confirms:
the `v0.1.0` tag resolves to the exact released commit; the GitHub
Release is not a draft and both named assets' digests match; PyPI
reports `enginery 0.1.0` with both artifacts' `sha256` digests matching.

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

## Final verdict

**GO.** `v0.1.0` is tagged from the CI-verified `origin/main` commit;
PyPI and the GitHub Release both contain the intended artifacts with
matching hashes; a clean install from the real PyPI index passes the
CLI and Stage 1 workflow smoke; all release-prep branches were merged
and are cleaned up; `RELEASE_NOTES.md` correctly scopes this release to
Stage 1 only and discloses the Stage 4 gate deferral with no committed
date.
