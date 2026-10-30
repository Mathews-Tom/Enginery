# Release Evidence

Post-publication evidence for every Enginery release-preparation pass.
Every result below was directly executed and observed in its own
releasing session; none is inferred or copied from a prior report.
Consolidated into one file, newest first, replacing the previous
`release-evidence-v0.1.0.md`/`release-evidence-v0.2.0.md`/`release-evidence-v0.3.0.md`
per-version files — each version's own evidence is fully preserved
below, not summarized away.

## `v0.9.0`

Post-publication evidence for the M37b release-preparation pass on 2026-10-30 (UTC), recorded in the same session from the retained receipts.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#246](https://github.com/Mathews-Tom/Enginery/pull/246) | `build(release): prepare v0.9.0` | `150feb20e640ee706034dfa1169858d777850baf` |
| [#247](https://github.com/Mathews-Tom/Enginery/pull/247) | `test(release): cover evidence --verify on an unwritable ledger in the consumer smoke` | `eeb9b9d64697bc708fd6bb1ffa200a1440d6dfe0` |

The release train also carries RF2 (#243, #244: the run request and its codec move to `enginery.workflows.run_intent`; stage-numbered test modules renamed) and FX1 (#245: `engine evidence --verify` rejects malformed artifact references and names ledgers it cannot read). Before and after RF2, the same disposable smoke at a fixed root produced request digest `sha256:42466fba58554f32d71b0c3a319eec1e94752d0d1c39717dea80e8ab14c5a73b`, restart aggregate version 1, tamper rejection and next action `qualify`.

In a clean detached worktree of the tagged commit, `uv run python scripts/release_gate.py --version 0.9.0` passed (docs currency, version consistency, `uv build`, `twine check` and a clean wheel install smoke). `uv run python scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` also passed (`stage1_runs=2 stage2_evidence=yes stage3_evidence=yes`, evidence digest `sha256:3228d475f2dd250fe698db3ed373f78c78fb285f6a8ddadfdd377f4972607356`). The `main` CI run [41052895258](https://github.com/Mathews-Tom/Enginery/actions/runs/41052895258) for that commit concluded `success`. Before the tag, the built wheel and sdist were installed into fresh environments on macOS arm64 (Python 3.12.15) and in a Lima Ubuntu 24.04.5 aarch64 VM (Python 3.12.15). `scripts/release_consumer_smoke.py` passed for both artifacts on both platforms, including the new check that `engine evidence --verify` on a WAL ledger in an unwritable directory exits 3 with a named error.

### Tag and artifacts

Annotated tag `v0.9.0` (object `3f52f13990139154dacc71a2b8a163f88817b6b0`, created 2026-10-30T13:05:13Z) targets `eeb9b9d64697bc708fd6bb1ffa200a1440d6dfe0`.

| Artifact | `sha256` |
|---|---|
| `enginery-0.9.0-py3-none-any.whl` | `2d9eb18771a8812df915e74408418c2e2a2217fefd5ce9660422b217382bd618` |
| `enginery-0.9.0.tar.gz` | `d07d90847a620f0e5f826e730a3da816be7205a6007d74a02f7e803c27706a70` |

`release_gate.py` produced these hashes on the tagged commit. The same hashes are on the GitHub Release's uploaded assets and in PyPI's record of the uploaded files.

The tagged README is the PyPI long description, so the published 0.9.0 metadata and the tagged tree still read "`v0.9.0` is the current release candidate". Status lines change to "published" only in this later documentation commit on `main`, because the tagged artifacts had to exist before publication could be recorded.

### Publication and consumer verification

- **PyPI:** <https://pypi.org/project/enginery/0.9.0/>. The wheel was uploaded at 2026-10-30T13:05:24Z and the sdist at 2026-10-30T13:05:26Z, using `twine` with the `[pypi]` entry of `~/.pypirc`.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.9.0>, published 2026-10-30T13:05:36Z, non-draft and not a prerelease, with exactly the wheel and the sdist as uploaded assets. GitHub also lists its own generated source-code archives, which were not uploaded and are not covered by these hashes.
- **Human approval:** the owner's message "Approved. Proceed with the recommended next steps." followed exposure of the exact candidate hashes and preceded the tag, PyPI upload and GitHub Release.

`uv run python scripts/verify_published_release.py --confirm-published ...` independently verified the GitHub Release target and both the GitHub and PyPI artifact hashes (`PASS verify-published-release version=0.9.0 tag=v0.9.0`). No-project consumers installed `enginery==0.9.0` from PyPI on macOS arm64 (Python 3.12.15) and in the Lima Ubuntu 24.04.5 aarch64 VM (Python 3.12.15), and `scripts/release_consumer_smoke.py --package enginery==0.9.0` passed on both, including the unwritable-ledger check and the 20-assertion `installed-manual-path` workflow.

### Not claimed

`v0.9.0` is a maintainability and defect-fix release: it adds no workflow capability and makes no claim of usefulness on real projects. Both agent hosts remain `experimental`. Ubuntu consumer proof uses a Lima VM, not supported-host qualification. G4 remains fail-closed and Stage 4 self-improvement remains unimplemented with no committed date.

## `v0.8.0`

Post-publication evidence for the M36b release-preparation pass on 2026-10-27 (UTC), recorded in the same session from the retained receipts.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#239](https://github.com/Mathews-Tom/Enginery/pull/239) | `build(release): prepare v0.8.0` | `5ffcac5da86c583bf96d47b311460d9521a3d625` |
| [#240](https://github.com/Mathews-Tom/Enginery/pull/240) | `test(release): cover v0.8.0 commands in the consumer smoke` | `00a4d5b82e7a09172357b2620b41afeb57f95e4e` |

The release train also carries RF1 (#225–#228), M34 (#229–#231, #233) and M36 (#234–#236, #238). In M36, the feature `engine evidence --verify` (issue #232) was delivered by Enginery's own factory run `fr-a5731e760f437c0aecca` as PR #238. After the owner's `pull_request.open` decision and merge, the owner upgraded the installed runtime through `scripts/enginery_runtime.py`.

In a clean detached worktree of the tagged commit, `uv run python scripts/release_gate.py --version 0.8.0` passed (docs currency, version consistency, `uv build`, `twine check` and a clean wheel install smoke). `uv run python scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` also passed (`stage1_runs=2 stage2_evidence=yes stage3_evidence=yes`, evidence digest `sha256:7e1e6c936ee7fc87696dd47d8e2cb3e28f6595837f91fcf7dbffcd141fd50705`). The `main` CI run [40425039582](https://github.com/Mathews-Tom/Enginery/actions/runs/40425039582) for that commit concluded `success`. Before the tag, the built wheel and sdist were installed into fresh environments on macOS arm64 (Python 3.12.15) and in a Lima Ubuntu 24.04.5 aarch64 VM (Python 3.12.15). `scripts/release_consumer_smoke.py` passed for each artifact on both platforms, including the 20-assertion `installed-manual-path` workflow and the new `engine evidence --verify` checks.

### Tag and artifacts

Annotated tag `v0.8.0` (object `558ff9c6bf26f2f72b913d93d560c0871db9f935`, created 2026-10-27T00:54:57Z) targets `00a4d5b82e7a09172357b2620b41afeb57f95e4e`.

| Artifact | `sha256` |
|---|---|
| `enginery-0.8.0-py3-none-any.whl` | `b74b4d2326d65300828510481627ea6b409973cc005c27951ba21323f1568932` |
| `enginery-0.8.0.tar.gz` | `bc70eedb4624a670df69aa77300c99dd9229902e0e2fff471cccb1e17b186c2e` |

`release_gate.py` produced these hashes on the tagged commit. The same hashes are on the GitHub Release assets and in PyPI's record of the uploaded files.

### Publication and consumer verification

- **PyPI:** <https://pypi.org/project/enginery/0.8.0/>. The wheel was uploaded at 2026-10-27T00:55:17Z and the sdist at 2026-10-27T00:55:20Z, using `twine` with the `[pypi]` entry of `~/.pypirc`. An earlier `uv publish` attempt with an ambient token was rejected with `403` before any file was accepted.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.8.0>, published 2026-10-27T00:55:33Z, non-draft and not a prerelease, with exactly the wheel and the sdist as assets.
- **Human approval:** the owner's message "Approved. Proceed with the recommended next steps." followed exposure of the exact candidate hashes and preceded the tag, PyPI upload and GitHub Release.

`uv run python scripts/verify_published_release.py --confirm-published ...` independently verified the GitHub Release target and both the GitHub and PyPI artifact hashes (`PASS verify-published-release version=0.8.0 tag=v0.8.0`). No-project consumers installed `enginery==0.8.0` from PyPI on macOS arm64 (Python 3.12.15) and in the Lima Ubuntu 24.04.5 aarch64 VM (Python 3.12.15), and `scripts/release_consumer_smoke.py --package enginery==0.8.0` passed on both, including the 20-assertion `installed-manual-path` workflow.

### Not claimed

The M36 factory run delivered one small Enginery feature on Enginery's own repository, using 4 of 4 allowed worker sessions. That is one self-development delivery, not evidence of usefulness on other real projects. Both agent hosts remain `experimental`. Ubuntu consumer proof uses a Lima VM, not supported-host qualification. G4 remains fail-closed and Stage 4 self-improvement remains unimplemented with no committed date.

## `v0.7.0`

Post-publication evidence for the M33b release-preparation pass on 2026-10-20 (UTC), recorded on 2026-10-26 from the receipts retained by that pass.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#221](https://github.com/Mathews-Tom/Enginery/pull/221) | `build(release): prepare coherent metadata and consumer gates` | `e6c873f508e29ae70f605a839ed8858fdf5337ff` |
| [#222](https://github.com/Mathews-Tom/Enginery/pull/222) | `test(release): prove clean platform consumer workflows` | `c53434630800177dde2aaade4aa339becfa30305` |
| [#223](https://github.com/Mathews-Tom/Enginery/pull/223) | `docs(release): finalize truthful release notes` | `1bcec96a9e723f9cd422d26e2ac596207f600803` |

The tagged release commit passed `uv run python scripts/release_gate.py --version 0.7.0` (docs currency, version consistency, `uv build`, `twine check`, and a clean wheel install smoke) and `uv run python scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` (`stage1_runs=2 stage2_evidence=yes stage3_evidence=yes`, evidence digest `sha256:59816e69246f36c6404d06eca9d00c9293bf9b65dfa2e7283c1217c5c3754ddb`). The `main` CI run [39507533653](https://github.com/Mathews-Tom/Enginery/actions/runs/39507533653) for that commit concluded `success`. Before the tag, the built wheel and sdist were installed into fresh environments on macOS arm64 (Python 3.12.8) and in a Lima Ubuntu 24.04.5 aarch64 VM (Python 3.12.15), and `scripts/release_consumer_smoke.py` passed on all four cells, each including the 20-assertion `installed-manual-path` workflow.

### Tag and artifacts

Annotated tag `v0.7.0` (object `66a5087c85483fa7307eeaffbf3f116a26e5f507`, created 2026-10-20T23:31:54Z) targets `1bcec96a9e723f9cd422d26e2ac596207f600803`.

| Artifact | `sha256` |
|---|---|
| `enginery-0.7.0-py3-none-any.whl` | `13b30960113f59820bd2cd805cb924b6ff1031032ef4fd759832a1aed2252e3f` |
| `enginery-0.7.0.tar.gz` | `c196e4f6301976f4c93ac909a7a65d5a5a16553ad0e66589db12344d5fa76097` |

The same hashes were produced by `release_gate.py` on the tagged commit, attached to the GitHub Release, and reported by PyPI for the uploaded files.

### Publication and consumer verification

- **PyPI:** <https://pypi.org/project/enginery/0.7.0/>. The sdist was uploaded at 2026-10-20T23:32:13Z and the wheel at 2026-10-20T23:32:16Z.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.7.0>, published 2026-10-20T23:32:06Z with exactly the wheel and the sdist as assets.
- **Human approval:** the owner's message "Approved. Proceed with the recommended next steps." followed exposure of the exact candidate hashes and preceded the tag, GitHub Release, and PyPI upload.

`uv run python scripts/verify_published_release.py --confirm-published ...`, re-run on 2026-10-26 while recording this section, independently verified the non-draft GitHub Release target and both GitHub and PyPI artifact hashes (`PASS verify-published-release version=0.7.0 tag=v0.7.0`). No-project consumers installed `enginery==0.7.0` from PyPI on macOS arm64 (Python 3.12.8) and in the Lima Ubuntu 24.04.5 aarch64 VM (Python 3.12.15), and `scripts/release_consumer_smoke.py --package enginery==0.7.0` passed on both, including the 20-assertion `installed-manual-path` workflow. The wheel with the published hash passed the same smoke again on both platforms after publication.

### Not claimed

Live qualification of the reference harness lifecycle and the manual bootstrap (M33, PRs #218–#220) ran against a designated synthetic qualification repository; it proves mechanics, not usefulness on real projects. A live requalification of the published wheel against the same synthetic repository was attempted on 2026-10-21 and ended `hold` (the live implementation failed and no replacement worker was allowed), so no live qualification is claimed for the published artifact; its local runner-boundary check passed 64/64 assertions. Both agent hosts remain `experimental`. Ubuntu consumer proof uses a Lima VM, not supported-host qualification. G4 remains fail-closed and Stage 4 self-improvement remains unimplemented with no committed date.

### Final verdict

**GO.** `v0.7.0` is tagged from verified main and published on PyPI and GitHub Releases with matching wheel and sdist hashes. It adds no workflow stage.

## `v0.6.0`

Post-publication evidence for the M27b release-preparation pass on 2026-10-13.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#188](https://github.com/Mathews-Tom/Enginery/pull/188) | `build(release): prepare coherent metadata and consumer gates` | `4ee346e2e7c9671a35c27a3864e724b45ecc0552` |
| [#189](https://github.com/Mathews-Tom/Enginery/pull/189) | `test(release): prove clean platform consumer workflows` | `3243d5b9e3f34171768800c52448da5f5d16cf8e` |
| [#190](https://github.com/Mathews-Tom/Enginery/pull/190) | `docs(release): finalize truthful release notes` | `eb009583909bfbe92f7ebaaeaad0779297e09520` |

The tagged release commit passed `uv run python scripts/release_gate.py --version 0.6.0` (docs currency, version consistency, `uv build`, `twine check`, and a clean wheel install smoke) and `uv run python scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` (`stage1_runs=2 stage2_evidence=yes stage3_evidence=yes`, evidence digest `sha256:50d83adb5b7c954d87ddb7a6484c306da73cb791557670af918433e2c7b6882d`). The `main` CI run [38342582774](https://github.com/Mathews-Tom/Enginery/actions/runs/38342582774) for that commit concluded `success`. Before the tag, the built wheel and sdist were installed into fresh environments on macOS and in clean `ubuntu:24.04` containers (wheel and sdist on each), and `scripts/release_consumer_smoke.py` passed on all four: `enginery --version`, `enginery doctor`, the `--model`/`--effort`/`--strict-effective` options, rejection of unsupported pins with exit code 2 and no file written, requests whose execution selection is bound to the packaged catalog, the packaged acceptance baseline and execution catalog, and fail-closed `enginery gate status --gate G4 --json`.

### Tag and artifacts

Annotated tag `v0.6.0` (object `54473f4b76ff86f421e7615c8490472369770e2a`) targets `eb009583909bfbe92f7ebaaeaad0779297e09520`.

| Artifact | `sha256` |
|---|---|
| `enginery-0.6.0-py3-none-any.whl` | `3868886564c5b3c2a27296889ea591446c96b867ffb1221f8b970e38654a1e5d` |
| `enginery-0.6.0.tar.gz` | `dffc23e1a95b37e1f120a609593cb03f3ee6b75304bbd53031cb364a088564c1` |

The same hashes were produced by `release_gate.py` on the tagged commit and reported by PyPI for the uploaded files.

### Publication and consumer verification

- **PyPI:** <https://pypi.org/project/enginery/0.6.0/>. The wheel was uploaded at 2026-10-13T03:40:46Z and the sdist at 2026-10-13T03:40:49Z.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.6.0>, published 2026-10-13T03:41:01Z with exactly the wheel and the sdist as assets.
- **Human approval:** the owner's messages "Approved for the release of v0.6.0" and "go ahead with the release right now" preceded the tag, PyPI upload, and GitHub Release.

`uv run python scripts/verify_published_release.py --confirm-published ...` independently verified the non-draft GitHub Release target and both GitHub and PyPI artifact hashes (`PASS verify-published-release version=0.6.0 tag=v0.6.0`). No-project, no-cache consumers installed `enginery==0.6.0` from PyPI on macOS (`uv`, Python 3.12.8) and in a clean `ubuntu:24.04` container (`pip`, Python 3.12.3), and `scripts/release_consumer_smoke.py --package enginery==0.6.0` passed on both with the same checks as above.

### Not claimed

This release claims no isolated-runner, live-host, bootstrap, or factory-path qualification, and adds no `enginery factory` command. Both agent hosts remain `experimental`. Ubuntu consumer proof uses a clean `ubuntu:24.04` container, not supported-host qualification. G4 remains fail-closed and Stage 4 self-improvement remains unimplemented with no committed date.

### Final verdict

**GO.** `v0.6.0` is tagged from verified main and published on PyPI and GitHub Releases with matching wheel and sdist hashes. It adds no workflow stage.

## `v0.5.0`

Post-publication evidence for the M24b release-preparation pass on 2026-08-11.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#174](https://github.com/Mathews-Tom/Enginery/pull/174) | `fix(release): select public documentation for currency checks` | `3959003c512ba2eac4755daeedcb5cad6c58bcb1` |
| [#175](https://github.com/Mathews-Tom/Enginery/pull/175) | `docs: sync v0.5.0 operator documentation` | `ff20fbc` |
| [#176](https://github.com/Mathews-Tom/Enginery/pull/176) | `test(release): prove v0.5.0 consumer installation` | `a6fac31492c578c239bdfb4494966c8428a1e49d` |
| [#177](https://github.com/Mathews-Tom/Enginery/pull/177) | `docs(release): finalize v0.5.0 notes` | `739c8d32acc7c4b4c4cce41f0ddb1e915c2fb678` |

The tagged release commit passed `uv run python scripts/release_gate.py --version 0.5.0`, `uv build`, and `uvx twine check dist/*`. Ruff, strict mypy, and pytest passed; pytest reported 1,490 passed and one opt-in live-provider test skipped. macOS and real Ubuntu 24.04 clean wheel and sdist installs passed `enginery --version`, `enginery doctor`, and fail-closed `enginery gate status --gate G4 --json`.

### Tag and artifacts

Annotated tag `v0.5.0` targets `739c8d32acc7c4b4c4cce41f0ddb1e915c2fb678`.

| Artifact | `sha256` |
|---|---|
| `enginery-0.5.0-py3-none-any.whl` | `360fe49b81f7093492aa69ad47735f3edc9bb4de8cd4037da24f2d55411b932c` |
| `enginery-0.5.0.tar.gz` | `00b1320280fd608fa97aa2ed2af3c7eaeffa0b7f22ab80880dd258f0e0e9b3a3` |

### Publication and consumer verification

- **PyPI:** <https://pypi.org/project/enginery/0.5.0/>.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.5.0>.
- **Human approval:** the `ask` tool recorded “Approve publication” immediately before the tag, PyPI upload, and GitHub Release.

`uv run python scripts/verify_published_release.py --confirm-published ...` independently verified the non-draft GitHub Release target and both GitHub/PyPI artifact hashes. A no-cache, isolated, no-project PyPI consumer ran `enginery --version`, both G4 deficiency command helps, labeled Stage 1 request help, and bare G4 status; G4 returned exit code `3` with `"overall": "fail"`.

### Final verdict

**GO.** `v0.5.0` is tagged from verified main and published on PyPI and GitHub Releases with matching wheel and sdist hashes. It adds no workflow stage. G4 remains fail-closed until real multi-repository, dual-human numeric-identity, intervention, outcome, and deficiency evidence exists; M14b/M15 remain blocked and Stage 4 self-improvement remains unimplemented.

## `v0.4.0`

Post-publication evidence for the 2026-08-03 release-preparation pass.

### Release-prep PR stack

| PR | Title | Merged commit |
|---|---|---|
| [#151](https://github.com/Mathews-Tom/Enginery/pull/151) | `fix(release): catch README version transitions` | `2ae0fd9b731a3a42ec3e80586db0b7ee7b2a6af3` |
| [#152](https://github.com/Mathews-Tom/Enginery/pull/152) | `build(release): prepare v0.4.0 metadata` | `2d529f75dd10ff9313be8c4e4692b5417db94982` |
| [#153](https://github.com/Mathews-Tom/Enginery/pull/153) | `docs: sync v0.4.0 operator documentation` | `5de64aa943bf070c8284938c801330f44ec81023` |
| [#154](https://github.com/Mathews-Tom/Enginery/pull/154) | `docs(release): finalize v0.4.0 notes` | `adbd47300ac38298a291386653a43bf85dea34bd` |

The corrective PR proved the documentation-currency guard against the tracked corpus: with canonical version `0.4.0`, it reported `README.md:13`, `docs/operations.md:10`, and `docs/operations.md:57` as stale `v0.3.0` self-declarations. After the documentation synchronization, `uv run python scripts/check_docs_currency.py` reported `PASS docs-currency`.

The final release commit `adbd47300ac38298a291386653a43bf85dea34bd` passed the full local release gate: 1,450 tests passed and one live-provider test was skipped because it requires explicit authorization; lint, formatting, strict typing, project-identity, fault-injection, and all import-boundary checks passed. `scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` passed with `stage1_runs=2`, Stage 2/3 evidence, and digest `sha256:83800a6a4bd3d743e8e585d45e47c237fa908759e234a7238264511215aace36`. The final documentation and release-notes PRs each passed their macOS and Ubuntu CI gates.

### Tag and artifacts

Annotated tag `v0.4.0` (object `23984f52ce66d76221bbe89b2c371c6d5e8f5390`) targets `adbd47300ac38298a291386653a43bf85dea34bd`.

| Artifact | `sha256` |
|---|---|
| `enginery-0.4.0-py3-none-any.whl` | `b29463f4d510b94329e95ce73f9f976b7b5fe5041f48a756230872e4df063390` |
| `enginery-0.4.0.tar.gz` | `37d97f4beeefc44a9888f6365581712949955d7279de346010375b4d072e3ce9` |

`uv build` ran once from the exact tagged commit. `uvx twine check dist/*` passed for both artifacts.

### Clean-install and published-package verification

Wheel and sdist clean installs each passed `enginery --version`, `enginery doctor`, and `scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` on macOS (Apple M1 Pro, Python 3.12.8) and real Ubuntu 24.04 containers (arm64, Python 3.12.13). Every environment was a fresh virtual environment; the Ubuntu checks used the published local release artifacts only, never an editable install.

From an empty scratch directory, `uv --no-cache run --isolated --no-project --with enginery==0.4.0` resolved the public PyPI package and ran `enginery --version`, `doctor`, `gate status --gate G4`, `stage1 build-request --help`, `workspace inspect --help`, `workspace release --help`, and `adapter doctor --json`. Gate G4 correctly returned exit code `3` with `overall: fail` for its unmeasured and unmet prerequisites. The adapter report included the `github-release` and `pypi` Stage 2 brokers and the `local-deployment-fixture` Stage 3 broker; the fixture was `misconfigured` in the empty scratch directory because its local application script was intentionally absent.

A separate fresh PyPI consumer then passed `scripts/full_system_gate.py --stages 1,2,3 --restart-between-stages` through `uv --no-cache run --isolated --no-project --with enginery==0.4.0`; it reported `stage1_runs=2`, Stage 2/3 evidence, and digest `sha256:b7701c79f949d5ccc00376f17b846aeba20175213434dc00cfe1f92f940c1494`. The source-only gate driver was used from the repository because workflow-gate fixtures are not distribution contents; Python imported `enginery==0.4.0` from PyPI rather than the checkout.

### Publication and approval

- **PyPI:** <https://pypi.org/project/enginery/0.4.0/>. `uv publish` uploaded the two explicitly named artifacts; the public version JSON reports both hashes above.
- **GitHub Release:** <https://github.com/Mathews-Tom/Enginery/releases/tag/v0.4.0>. It is neither draft nor prerelease and attaches the same two digest-matching artifacts.
- **Human approval:** the `ask` tool recorded “Approve publication” before the annotated tag, PyPI upload, and GitHub Release were created.

`uv run python scripts/verify_published_release.py --confirm-published ...` passed, independently confirming the tag target, non-draft GitHub Release, both GitHub asset digests, and both PyPI artifact digests.

### Final verdict

**GO.** `v0.4.0` is tagged from the verified release commit and is published on PyPI and GitHub Releases with matching wheel and sdist hashes. Clean local artifacts and the public PyPI package exercise the released CLI surface. The release adds no workflow stage; Stage 4 self-improvement remains unimplemented and gate-deferred behind G4 with no committed date.

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
