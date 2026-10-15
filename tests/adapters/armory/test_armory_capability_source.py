from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from tests.support.capabilities import package_files, project_source, wrap_source

from enginery.adapters.armory import ArmoryCapabilitySource
from enginery.application.adapter_types import AdapterAvailability
from enginery.capabilities.closure import MAX_FILE_BYTES, build_closure
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityAssetTypeError,
    CapabilityClosureLimitError,
    CapabilityError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
    CapabilityPathError,
    CapabilitySymlinkError,
)
from enginery.capabilities.lock import ApprovedCapabilities, ProvenanceStatus
from enginery.capabilities.materialize import materialize_lock
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.domain.errors import (
    InvalidInputError,
    MissingPrerequisiteError,
    TransientProviderFailureError,
)

_REPO = "Mathews-Tom/armory"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_COMMIT_URL = f"https://api.github.com/repos/{_REPO}/commits/main"
_TREE_URL = f"https://api.github.com/repos/{_REPO}/git/trees/{_SHA}?recursive=1"
_PACKAGE = "skills/adr-writer"
_LICENSE_TEXT = (
    b"MIT License\n\nCopyright (c) 2026 Tom\n\n"
    b"Permission is hereby granted, free of charge, to any person obtaining a copy\n"
    b'of this software and associated documentation files (the "Software"), to deal\n'
    b"in the Software without restriction, including without limitation the rights\n"
    b"to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
    b"copies of the Software, and to permit persons to whom the Software is\n"
    b"furnished to do so, subject to the following conditions:\n\n"
    b"The above copyright notice and this permission notice shall be included in all\n"
    b"copies or substantial portions of the Software.\n\n"
    b'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
    b"IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
    b"FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
    b"AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
    b"LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
    b"OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
    b"SOFTWARE.\n"
)
_FILES: dict[str, bytes] = {
    f"{_PACKAGE}/SKILL.md": b"# ADR writer skill body",
    f"{_PACKAGE}/references/adr-template.md": b"template",
    f"{_PACKAGE}/scripts/check.py": b"print('check')\n",
    f"{_PACKAGE}/evals/cases.yaml": b"cases: []\n",
    "skills/adr-writer-extra/SKILL.md": b"a sibling package that must not leak in",
    "README.md": b"# armory",
}


def _raw(path: str) -> str:
    return f"https://raw.githubusercontent.com/{_REPO}/{_SHA}/{quote(path, safe='/')}"


def _manifest_yaml(path: str = _PACKAGE) -> bytes:
    document = {
        "packages": {
            "skills": [
                {"name": "adr-writer", "version": "1.1.1", "path": path},
                {"name": "missing-path", "version": "1.0.0"},
            ]
        }
    }
    return yaml.safe_dump(document).encode("utf-8")


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def _tree_json(
    files: dict[str, bytes],
    *,
    modes: dict[str, str] | None = None,
    sizes: dict[str, int] | None = None,
    truncated: bool = False,
) -> bytes:
    modes = modes or {}
    sizes = sizes or {}
    entries = [
        {
            "path": path,
            "mode": modes.get(path, "100644"),
            "type": "blob",
            "size": sizes.get(path, len(content)),
            "sha": _blob_sha(content),
        }
        for path, content in sorted(files.items())
    ]
    entries.append({"path": "skills", "mode": "040000", "type": "tree", "sha": "x"})
    return json.dumps({"sha": _SHA, "tree": entries, "truncated": truncated}).encode("utf-8")


class _Fetcher:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        del timeout
        self.urls.append(url)
        if url not in self.routes:
            raise TimeoutError(f"no fixture route for {url}")
        return self.routes[url]


def _metadata(
    license_text: bytes | None = _LICENSE_TEXT, manifest_path: str = _PACKAGE
) -> dict[str, bytes]:
    metadata = {"manifest.yaml": _manifest_yaml(manifest_path)}
    if license_text is not None:
        metadata["LICENSE"] = license_text
    return metadata


def _routes(
    files: dict[str, bytes] | None = None,
    *,
    modes: dict[str, str] | None = None,
    manifest_path: str = _PACKAGE,
    license_text: bytes | None = _LICENSE_TEXT,
) -> dict[str, bytes]:
    files = dict(_FILES if files is None else files)
    tree_files = {**files, **_metadata(license_text, manifest_path)}
    routes = {
        _COMMIT_URL: json.dumps({"sha": _SHA}).encode("utf-8"),
        _TREE_URL: _tree_json(tree_files, modes=modes),
    }
    routes.update({_raw(path): content for path, content in tree_files.items()})
    return routes


def _source(
    routes: dict[str, bytes] | None = None, *, revision: str | None = None
) -> tuple[ArmoryCapabilitySource, _Fetcher]:
    fetcher = _Fetcher(_routes() if routes is None else routes)
    return ArmoryCapabilitySource(fetcher=fetcher, revision=revision), fetcher


def test_probe_reports_available_when_the_catalog_is_reachable() -> None:
    source, _ = _source()

    status = source.probe()

    assert status.availability is AdapterAvailability.AVAILABLE
    assert status.fingerprint is not None
    assert _SHA in status.detail


def test_probe_reports_unavailable_without_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml":
            raise ImportError("no module named yaml")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    source, _ = _source()

    status = source.probe()

    assert status.availability is AdapterAvailability.UNAVAILABLE
    assert "armory" in status.detail.lower()


def test_a_missing_armory_extra_is_a_prerequisite_error_and_a_shadowed_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml":
            raise ImportError("no module named yaml")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    source, _ = _source()
    files = package_files("adr-writer", "1.1.1")
    resolver = CapabilityResolver([project_source(files), _armory(source)])

    with pytest.raises(MissingPrerequisiteError, match="pyyaml"):
        source.resolve("adr-writer", "1.1.1")
    resolved = resolver.resolve_with_files(
        [CapabilityRequest(name="adr-writer", version="1.1.1")], report_shadowed=True
    )

    (candidate,) = resolved.selections["adr-writer"].shadowed
    assert candidate.kind is SourceKind.ARMORY
    assert candidate.problem is not None
    assert "pyyaml" in candidate.problem


def test_probe_reports_misconfigured_for_an_unreachable_catalog() -> None:
    source, _ = _source({})

    assert source.probe().availability is AdapterAvailability.MISCONFIGURED


def test_probe_reports_misconfigured_for_a_tampered_catalog_manifest() -> None:
    routes = _routes()
    routes[_raw("manifest.yaml")] = _manifest_yaml("skills/adr-writer-extra")
    source, _ = _source(routes)

    assert source.probe().availability is AdapterAvailability.MISCONFIGURED


def test_resolve_returns_a_closure_addressed_descriptor_bound_to_the_commit() -> None:
    source, _ = _source()

    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.revision == _SHA
    assert descriptor.provenance == "armory"
    assert descriptor.license == "MIT"
    assert descriptor.digest == build_closure(source.fetch_closure("adr-writer", "1.1.1")).digest


def test_every_read_is_bound_to_the_resolved_commit_never_the_mutable_ref() -> None:
    source, fetcher = _source()

    source.resolve("adr-writer", "1.1.1")

    raw_reads = [url for url in fetcher.urls if "raw.githubusercontent.com" in url]
    assert raw_reads
    assert all(f"/{_SHA}/" in url for url in raw_reads)
    assert not any("/main/" in url for url in raw_reads)


def test_an_explicit_revision_skips_ref_resolution() -> None:
    source, fetcher = _source(revision=_SHA)

    assert source.resolve("adr-writer", "1.1.1") is not None
    assert _COMMIT_URL not in fetcher.urls


def test_fetch_closure_returns_every_package_file_with_relative_paths() -> None:
    source, _ = _source()

    files = {item.path: item for item in source.fetch_closure("adr-writer", "1.1.1")}

    assert set(files) == {
        "SKILL.md",
        "references/adr-template.md",
        "scripts/check.py",
        "evals/cases.yaml",
        "capability.json",
    }
    assert files["SKILL.md"].content == b"# ADR writer skill body"
    assert files["scripts/check.py"].executable is False


def test_a_sibling_package_sharing_a_name_prefix_is_not_swept_into_the_closure() -> None:
    source, _ = _source()

    paths = {item.path for item in source.fetch_closure("adr-writer", "1.1.1")}

    assert not any("extra" in path for path in paths)


def test_the_executable_bit_comes_from_the_pinned_tree_mode() -> None:
    routes = _routes(modes={f"{_PACKAGE}/scripts/check.py": "100755"})
    source, _ = _source(routes)

    files = {item.path: item for item in source.fetch_closure("adr-writer", "1.1.1")}

    assert files["scripts/check.py"].executable is True


def test_a_synthesized_manifest_grants_no_authority_and_declares_no_command() -> None:
    source, _ = _source()

    closure = build_closure(source.fetch_closure("adr-writer", "1.1.1"))

    manifest = closure.manifest
    assert (manifest.name, manifest.version) == ("adr-writer", "1.1.1")
    assert manifest.kind.value == "instruction"
    assert manifest.permissions == ()
    assert manifest.dependencies == ()
    assert manifest.license is None


def test_a_package_shipped_manifest_is_used_instead_of_a_synthesized_one() -> None:
    shipped = json.dumps(
        {
            "schema_version": 1,
            "name": "adr-writer",
            "version": "1.1.1",
            "kind": "instruction",
            "license": "MIT",
            "permissions": ["repository.read"],
        }
    ).encode("utf-8")
    files = {**_FILES, f"{_PACKAGE}/capability.json": shipped}
    source, _ = _source(_routes(files))

    closure = build_closure(source.fetch_closure("adr-writer", "1.1.1"))

    assert [permission.value for permission in closure.manifest.permissions] == ["repository.read"]


def test_a_sibling_script_change_at_the_pinned_tree_changes_the_closure_digest() -> None:
    original, _ = _source()
    changed_files = {**_FILES, f"{_PACKAGE}/scripts/check.py": b"print('changed')\n"}
    changed, _ = _source(_routes(changed_files))

    first = original.resolve("adr-writer", "1.1.1")
    second = changed.resolve("adr-writer", "1.1.1")

    assert first is not None and second is not None
    assert first.digest != second.digest


def test_a_reference_change_at_the_pinned_tree_changes_the_closure_digest() -> None:
    original, _ = _source()
    changed_files = {**_FILES, f"{_PACKAGE}/references/adr-template.md": b"edited"}
    changed, _ = _source(_routes(changed_files))

    first = original.resolve("adr-writer", "1.1.1")
    second = changed.resolve("adr-writer", "1.1.1")

    assert first is not None and second is not None
    assert first.digest != second.digest


def test_resolve_skips_entries_missing_a_package_path() -> None:
    source, _ = _source()

    assert source.resolve("missing-path", "1.0.0") is None


def test_resolve_returns_none_for_an_unknown_capability() -> None:
    source, _ = _source()

    assert source.resolve("does-not-exist", "1.0.0") is None


def test_fetch_closure_raises_for_an_unknown_capability() -> None:
    source, _ = _source()

    with pytest.raises(MissingPrerequisiteError):
        source.fetch_closure("does-not-exist", "1.0.0")


def test_discover_lists_every_catalog_row_without_fetching_any_package() -> None:
    source, fetcher = _source()

    listings = source.discover()

    assert [(item.name, item.version) for item in listings] == [("adr-writer", "1.1.1")]
    assert all(item.provenance == "armory" and item.revision == _SHA for item in listings)
    assert _TREE_URL not in fetcher.urls
    assert not any(url.startswith(_raw(f"{_PACKAGE}/")) for url in fetcher.urls)
    assert not any(url.startswith(_raw("skills/")) for url in fetcher.urls)


def test_an_unlockable_package_does_not_break_discover_but_fails_loudly_on_resolve() -> None:
    bad_path = f"{_PACKAGE}/scripts/__pycache__/check.cpython-312.pyc"
    source, _ = _source(_routes({**_FILES, bad_path: b"\x00bytecode"}))

    assert [item.name for item in source.discover()] == ["adr-writer"]
    with pytest.raises(CapabilityAssetTypeError) as resolve_failure:
        source.resolve("adr-writer", "1.1.1")
    with pytest.raises(CapabilityAssetTypeError) as fetch_failure:
        source.fetch_closure("adr-writer", "1.1.1")

    for failure in (resolve_failure, fetch_failure):
        assert isinstance(failure.value, CapabilityError)
        assert "check.cpython-312.pyc" in str(failure.value.details["path"])


def test_a_traversing_catalog_path_is_rejected() -> None:
    source, _ = _source(_routes(manifest_path="../outside"))

    with pytest.raises(CapabilityPathError):
        source.resolve("adr-writer", "1.1.1")


def test_a_catalog_path_with_no_files_at_the_revision_is_a_missing_prerequisite() -> None:
    source, _ = _source(_routes(manifest_path="skills/not-there"))

    with pytest.raises(MissingPrerequisiteError, match="no files"):
        source.resolve("adr-writer", "1.1.1")


def test_a_symlink_in_the_pinned_tree_is_rejected() -> None:
    routes = _routes(modes={f"{_PACKAGE}/references/adr-template.md": "120000"})
    source, _ = _source(routes)

    with pytest.raises(CapabilitySymlinkError):
        source.resolve("adr-writer", "1.1.1")


def test_a_submodule_in_the_pinned_tree_is_rejected() -> None:
    routes = _routes()
    tree = json.loads(routes[_TREE_URL])
    tree["tree"].append(
        {"path": f"{_PACKAGE}/vendored", "mode": "160000", "type": "commit", "sha": "a" * 40}
    )
    routes[_TREE_URL] = json.dumps(tree).encode("utf-8")
    source, _ = _source(routes)

    with pytest.raises(CapabilitySymlinkError):
        source.resolve("adr-writer", "1.1.1")


def test_an_unreviewed_asset_type_makes_the_package_unlockable() -> None:
    files = {**_FILES, f"{_PACKAGE}/scripts/__pycache__/check.cpython-312.pyc": b"\x00bytecode"}
    source, _ = _source(_routes(files))

    with pytest.raises(CapabilityAssetTypeError):
        source.resolve("adr-writer", "1.1.1")


def test_a_truncated_file_listing_is_rejected() -> None:
    routes = _routes()
    routes[_TREE_URL] = _tree_json({**_FILES, **_metadata()}, truncated=True)
    source, _ = _source(routes)

    with pytest.raises(MissingPrerequisiteError, match="truncated"):
        source.resolve("adr-writer", "1.1.1")


def test_a_ref_that_does_not_resolve_to_a_commit_sha_is_rejected() -> None:
    routes = _routes()
    routes[_COMMIT_URL] = json.dumps({"sha": "not-a-sha"}).encode("utf-8")
    source, _ = _source(routes)

    with pytest.raises(MissingPrerequisiteError, match="commit sha"):
        source.resolve("adr-writer", "1.1.1")


@pytest.mark.parametrize("revision", ["main", "abc123", "G" * 40, "A" * 40])
def test_a_revision_must_be_a_full_lowercase_commit_sha(revision: str) -> None:
    with pytest.raises(InvalidInputError):
        ArmoryCapabilitySource(revision=revision)


def test_unreachable_package_content_raises_transient_failure() -> None:
    routes = _routes()
    del routes[_raw(f"{_PACKAGE}/SKILL.md")]
    source, _ = _source(routes)

    with pytest.raises(TransientProviderFailureError):
        source.resolve("adr-writer", "1.1.1")


def test_an_unreachable_license_is_reported_as_unknown() -> None:
    source, _ = _source(_routes(license_text=None))

    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.license == "unknown"


def _armory(source: ArmoryCapabilitySource) -> CapabilitySource:
    return wrap_source(source, SourceKind.ARMORY)


def test_armory_sourced_capability_is_always_unauthenticated_when_run_introduced() -> None:
    """No per-package signature exists today; every Armory capability a run
    introduces must stay unauthenticated regardless of a clean digest fetch."""

    source, _ = _source()

    lock = CapabilityResolver([_armory(source)]).resolve(
        [CapabilityRequest(name="adr-writer", version="1.1.1")]
    )

    entry = lock.get("adr-writer")
    assert entry is not None
    assert entry.introduced_by_run is True
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.revision == _SHA
    assert entry.requires_human_approval() is True


def test_approved_armory_package_materializes_its_complete_closure(tmp_path: Path) -> None:
    source, _ = _source()
    resolved = CapabilityResolver([_armory(source)]).resolve_with_files(
        [CapabilityRequest(name="adr-writer", version="1.1.1")]
    )

    with pytest.raises(CapabilityApprovalRequiredError):
        materialize_lock(resolved.lock, resolved.files_by_name, root=tmp_path)
    materialized = materialize_lock(
        resolved.lock,
        resolved.files_by_name,
        root=tmp_path,
        approvals=ApprovedCapabilities.for_entries(resolved.lock, ["adr-writer"]),
    )

    directory = materialized["adr-writer"]
    assert (directory / "SKILL.md").read_bytes() == b"# ADR writer skill body"
    assert (directory / "references" / "adr-template.md").read_bytes() == b"template"
    assert (directory / "scripts" / "check.py").read_bytes() == b"print('check')\n"
    assert (directory / "capability.json").is_file()


def test_armory_package_with_an_unknown_license_is_held_even_when_approved(tmp_path: Path) -> None:
    source, _ = _source(_routes(license_text=b"Some bespoke terms\n"))
    resolved = CapabilityResolver([_armory(source)]).resolve_with_files(
        [CapabilityRequest(name="adr-writer", version="1.1.1")]
    )

    with pytest.raises(CapabilityLicenseHoldError):
        materialize_lock(
            resolved.lock,
            resolved.files_by_name,
            root=tmp_path,
            approvals=ApprovedCapabilities.for_entries(resolved.lock, ["adr-writer"]),
        )


_APACHE_TEXT = (
    b"                                 Apache License\n"
    b"                           Version 2.0, January 2004\n"
    b"                        http://www.apache.org/licenses/\n\n"
    b"   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION\n"
)
_GPL_TEXT = (
    b"                    GNU GENERAL PUBLIC LICENSE\n"
    b"                       Version 3, 29 June 2007\n"
)


def _package_license(license_text: bytes, *, name: str = "LICENSE") -> str | None:
    files = {**_FILES, f"{_PACKAGE}/{name}": license_text}
    source, _ = _source(_routes(files))
    descriptor = source.resolve("adr-writer", "1.1.1")
    assert descriptor is not None
    return descriptor.license


def test_a_package_shipping_an_apache_license_inside_an_mit_repository_is_not_mit() -> None:
    assert _package_license(_APACHE_TEXT, name="LICENSE.txt") == "unknown"


def test_a_package_shipping_a_gpl_license_is_held_as_unknown() -> None:
    assert _package_license(_GPL_TEXT) == "unknown"


def test_a_package_shipping_an_unrecognized_license_text_is_unknown() -> None:
    assert _package_license(b"Do what you like, mostly.\n") == "unknown"


def test_a_package_license_that_only_mentions_a_known_name_is_unknown() -> None:
    text = b"See the MIT License for details; this package is also GNU General Public License.\n"

    assert _package_license(text) == "unknown"


def test_a_package_license_agreeing_with_the_repository_is_reported() -> None:
    assert _package_license(_LICENSE_TEXT, name="COPYING") == "MIT"


def test_a_package_shipping_a_gpl_copying_variant_is_unknown() -> None:
    assert _package_license(_GPL_TEXT, name="COPYING.txt") == "unknown"
    assert _package_license(_GPL_TEXT, name="COPYING.md") == "unknown"


def test_a_notice_that_defers_to_the_repository_license_keeps_the_repository_license() -> None:
    notice = b"The files in this package are under the repository license.\n"

    assert _package_license(notice, name="NOTICE.md") == "MIT"


def test_a_notice_carrying_another_license_marker_makes_the_license_unknown() -> None:
    notice = b"Portions of this package are available under the GPL.\n"

    assert _package_license(notice, name="NOTICE.txt") == "unknown"


def test_a_notice_reserving_all_rights_without_a_grant_makes_the_license_unknown() -> None:
    assert _package_license(b"Copyright Tom. All rights reserved.\n", name="NOTICE") == "unknown"


def test_mit_text_with_appended_terms_is_not_mit() -> None:
    text = _LICENSE_TEXT + b'\n"Commons Clause" License Condition v1.0: no selling the Software.\n'

    assert _package_license(text) == "unknown"


def test_mit_text_with_an_inserted_carve_out_is_not_mit() -> None:
    text = _LICENSE_TEXT.replace(
        b"copies or substantial portions of the Software.\n",
        b"copies or substantial portions of the Software.\n"
        b"This license does not permit use in commercial products.\n",
    )

    assert text != _LICENSE_TEXT
    assert _package_license(text) == "unknown"


def test_mit_text_with_terms_hidden_on_a_copyright_line_is_not_mit() -> None:
    text = _LICENSE_TEXT.replace(
        b"Copyright (c) 2026 Tom\n", b"Copyright (c) 2026 Tom. Free for educational use.\n"
    )

    assert _package_license(text) == "unknown"


def test_mit_text_with_a_different_copyright_holder_and_heading_markers_is_mit() -> None:
    text = _LICENSE_TEXT.replace(b"MIT License", b"# MIT License").replace(b"Tom", b"Acme & Co.")

    assert _package_license(text) == "MIT"


@pytest.mark.parametrize(
    "name", ["license.md", "LICENCE.md", "LICENSE.MD", "COPYING.MD", "License.rst", "licence"]
)
def test_license_files_with_other_casing_and_extensions_are_classified(name: str) -> None:
    assert _package_license(_LICENSE_TEXT, name=name) == "MIT"
    assert _package_license(_GPL_TEXT, name=name) == "unknown"
    assert _package_license(_LICENSE_TEXT + b"\nDual licensed.\n", name=name) == "unknown"


@pytest.mark.parametrize(
    "name",
    [
        "LICENSE-GPL.md",
        "LICENSE.gpl.md",
        "COPYING.LESSER.md",
        "THIRD_PARTY_NOTICES.md",
        "LICENSE_GPL.md",
        "UNLICENSE.md",
        "LICENSES.md",
        "\uff2c\uff29\uff23\uff25\uff2e\uff33\uff25.md",
        "LICENSE-MIT",
        "COPYRIGHT.txt",
    ],
)
def test_every_license_like_root_file_with_non_template_text_makes_the_license_unknown(
    name: str,
) -> None:
    assert _package_license(_GPL_TEXT, name=name) == "unknown"
    assert _package_license(_LICENSE_TEXT + b"\nSee also the LGPL.\n", name=name) == "unknown"
    assert _package_license(_LICENSE_TEXT, name=name) == "MIT"


@pytest.mark.parametrize(
    "line",
    [
        b"Copyright 2024 Foo. Files in vendor/ are GPLv3.",
        b"Copyright 2024 Foo. Selling copies needs a paid agreement with Foo.",
        b"Copyright (c) 2024 Foo; the Commons Clause v1.0 applies to this package.",
        b"(c) 2024 vendor/ code is AGPL-3.0",
        b"Copyright 2024 Foo <!-- sale needs a paid agreement; vendor/ is GPLv3 -->",
        "Copyright 2024 Foo. Com\u200bmercial u\u200bse prohi\u200bbited.".encode(),
    ],
)
def test_terms_smuggled_on_a_copyright_line_never_yield_mit(line: bytes) -> None:
    body = b"THE SOFTWARE IS PROVIDED"

    assert _package_license(line + b"\n" + _LICENSE_TEXT) == "unknown"
    assert _package_license(_LICENSE_TEXT.replace(body, line + b"\n" + body, 1)) == "unknown"


def test_license_lines_split_with_a_line_separator_never_yield_mit() -> None:
    text = _LICENSE_TEXT.replace(b"\n\n", "\u2028Commercial use prohibited\u2028".encode(), 1)

    assert _package_license(text) == "unknown"


@pytest.mark.parametrize(
    "notice",
    [
        b"This package is licensed under the GPLv3.\n",
        b"Parts are LGPLv2.1 licensed.\n",
        b"Network use is covered by the AGPLv3.\n",
        b"Released under GPL2.\n",
        b"Released under GPL-3.0-only.\n",
        b"Released under the SSPL.\n",
        b"Content is CC BY-NC 4.0.\n",
        b"Free for non-commercial use.\n",
        b"Free for noncommercial use.\n",
        b'Licensed with the "Commons Clause" condition.\n',
    ],
)
@pytest.mark.parametrize("name", ["NOTICE", "notice.md", "Notice.txt", "NOTICE.rst"])
def test_a_notice_naming_another_license_family_makes_the_license_unknown(
    notice: bytes, name: str
) -> None:
    assert _package_license(notice, name=name) == "unknown"


def test_a_notice_that_names_a_separate_unbundled_project_keeps_the_repository_license() -> None:
    notice = (
        b"# Attribution and terms\n\n## Hyperframes\n\n"
        b"This package drives the open-source Hyperframes command-line tool, licensed under "
        b"[Apache License 2.0](https://example.invalid/LICENSE). The tool is **not bundled or "
        b"redistributed** here. The templates, scripts, tests and references in this package "
        b"are original work released under the licence of the repository that contains them.\n"
    )

    assert _package_license(notice, name="NOTICE.md") == "MIT"


def test_a_notice_never_decides_the_license() -> None:
    files = {**_FILES, f"{_PACKAGE}/NOTICE": b"Original notice.\n"}
    source, _ = _source(_routes(files, license_text=None))
    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.license == "unknown"


def test_a_tampered_repository_license_is_unknown() -> None:
    routes = _routes()
    routes[_raw("LICENSE")] = _LICENSE_TEXT + b"\n"
    source, _ = _source(routes)

    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.license == "unknown"


def test_a_repository_license_missing_from_the_pinned_tree_is_unknown() -> None:
    routes = _routes()
    routes[_TREE_URL] = _tree_json({**_FILES, **_metadata(license_text=None)})
    routes[_raw("LICENSE")] = _LICENSE_TEXT
    source, _ = _source(routes)

    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.license == "unknown"


def test_a_catalog_manifest_that_does_not_match_the_pinned_tree_is_rejected() -> None:
    routes = _routes()
    routes[_raw("manifest.yaml")] = _manifest_yaml("skills/adr-writer-extra")
    source, _ = _source(routes)

    with pytest.raises(CapabilityIntegrityError):
        source.resolve("adr-writer", "1.1.1")


def test_a_package_without_a_license_file_uses_the_repository_license() -> None:
    source, _ = _source()

    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.license == "MIT"


def test_a_fetched_file_that_does_not_match_the_pinned_tree_sha_is_rejected() -> None:
    routes = _routes()
    routes[_raw(f"{_PACKAGE}/SKILL.md")] = b"# substituted content of the same kind"
    source, _ = _source(routes)

    with pytest.raises(CapabilityIntegrityError):
        source.resolve("adr-writer", "1.1.1")


def test_path_segments_are_url_quoted_when_fetching() -> None:
    files = {**_FILES, f"{_PACKAGE}/references/odd name?x#y.md": b"odd"}
    source, fetcher = _source(_routes(files))

    descriptor = source.resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert any(url.endswith("/references/odd%20name%3Fx%23y.md") for url in fetcher.urls)
    assert not any("?x#y" in url for url in fetcher.urls)


def test_a_package_over_the_total_size_limit_is_rejected_before_any_blob_is_fetched() -> None:
    extra = {f"{_PACKAGE}/data/part{index}.md": b"x" for index in range(9)}
    files = {**_FILES, **extra}
    routes = _routes(files)
    routes[_TREE_URL] = _tree_json(
        {**files, **_metadata()}, sizes=dict.fromkeys(extra, MAX_FILE_BYTES)
    )
    source, fetcher = _source(routes)

    with pytest.raises(CapabilityClosureLimitError):
        source.resolve("adr-writer", "1.1.1")

    assert not any("/data/" in url or "SKILL.md" in url for url in fetcher.urls)


def test_engine_resolves_capabilities_without_armory_installed() -> None:
    """The reviewed-base, local-only resolution path must not require Armory at all."""

    files = package_files("repository-read")
    resolver = CapabilityResolver([project_source(files)])

    lock = resolver.resolve([CapabilityRequest(name="repository-read", version="1")])

    entry = lock.get("repository-read")
    assert entry is not None
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.requires_human_approval() is False
