from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from tests.support.capabilities import descriptor_for, package_files

from enginery.adapters.armory import ArmoryCapabilitySource
from enginery.adapters.local import LocalCapabilitySource
from enginery.application.adapter_types import AdapterAvailability
from enginery.capabilities.closure import MAX_FILE_BYTES, build_closure
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityAssetTypeError,
    CapabilityClosureLimitError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
    CapabilityPathError,
    CapabilitySymlinkError,
)
from enginery.capabilities.lock import ProvenanceStatus
from enginery.capabilities.materialize import materialize_lock
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
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
    b"of this software and associated documentation files, to deal\n"
    b"in the Software without restriction.\n"
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


def _routes(
    files: dict[str, bytes] | None = None,
    *,
    modes: dict[str, str] | None = None,
    manifest_path: str = _PACKAGE,
    license_text: bytes | None = _LICENSE_TEXT,
) -> dict[str, bytes]:
    files = dict(_FILES if files is None else files)
    routes = {
        _COMMIT_URL: json.dumps({"sha": _SHA}).encode("utf-8"),
        _TREE_URL: _tree_json(files, modes=modes),
        _raw("manifest.yaml"): _manifest_yaml(manifest_path),
    }
    routes.update({_raw(path): content for path, content in files.items()})
    if license_text is not None:
        routes[_raw("LICENSE")] = license_text
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


def test_probe_reports_misconfigured_for_an_unreachable_catalog() -> None:
    source, _ = _source({})

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


def test_discover_returns_every_resolvable_entry() -> None:
    source, _ = _source()

    assert [d.name for d in source.discover()] == ["adr-writer"]


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
    routes[_TREE_URL] = _tree_json(_FILES, truncated=True)
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


def test_armory_sourced_capability_is_always_unauthenticated_when_run_introduced() -> None:
    """No per-package signature exists today; every Armory capability a run
    introduces must stay unauthenticated regardless of a clean digest fetch."""

    source, _ = _source()

    lock = CapabilityResolver([source]).resolve(
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
    resolved = CapabilityResolver([source]).resolve_with_files(
        [CapabilityRequest(name="adr-writer", version="1.1.1")]
    )

    with pytest.raises(CapabilityApprovalRequiredError):
        materialize_lock(resolved.lock, resolved.files_by_name, root=tmp_path)
    materialized = materialize_lock(
        resolved.lock,
        resolved.files_by_name,
        root=tmp_path,
        approved_names=frozenset({"adr-writer"}),
    )

    directory = materialized["adr-writer"]
    assert (directory / "SKILL.md").read_bytes() == b"# ADR writer skill body"
    assert (directory / "references" / "adr-template.md").read_bytes() == b"template"
    assert (directory / "scripts" / "check.py").read_bytes() == b"print('check')\n"
    assert (directory / "capability.json").is_file()


def test_armory_package_with_an_unknown_license_is_held_even_when_approved(tmp_path: Path) -> None:
    source, _ = _source(_routes(license_text=b"Some bespoke terms\n"))
    resolved = CapabilityResolver([source]).resolve_with_files(
        [CapabilityRequest(name="adr-writer", version="1.1.1")]
    )

    with pytest.raises(CapabilityLicenseHoldError):
        materialize_lock(
            resolved.lock,
            resolved.files_by_name,
            root=tmp_path,
            approved_names=frozenset({"adr-writer"}),
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
    routes[_TREE_URL] = _tree_json(files, sizes=dict.fromkeys(extra, MAX_FILE_BYTES))
    source, fetcher = _source(routes)

    with pytest.raises(CapabilityClosureLimitError):
        source.resolve("adr-writer", "1.1.1")

    assert not any("/data/" in url or "SKILL.md" in url for url in fetcher.urls)


def test_engine_resolves_capabilities_without_armory_installed() -> None:
    """The reviewed-base, local-only resolution path must not require Armory at all."""

    files = package_files("repository-read")
    descriptor = descriptor_for(files)
    local_source = LocalCapabilitySource(
        capabilities=(descriptor,), packages={("repository-read", "1"): files}
    )
    resolver = CapabilityResolver(
        [local_source], reviewed_base=frozenset({("repository-read", "1")})
    )

    lock = resolver.resolve([CapabilityRequest(name="repository-read", version="1")])

    entry = lock.get("repository-read")
    assert entry is not None
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.requires_human_approval() is False
